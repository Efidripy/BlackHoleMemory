"""Bounded local LongMemEval-S retrieval smoke using BHM lexical scoring.

The adapter consumes a pinned, already-admitted local dataset and emits only
content-free evaluation manifests, retrieval receipts and metrics. It does not
start BHM, call a model, or write SQLite, Qdrant or Mem0. The route is
explicitly lexical; it is evidence for the BHM lexical retrieval primitive,
not a claim about the full federated/vector production route.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Mapping

from . import app as bhm_app
from .external_evaluation_admission import ExternalEvaluationAdmissionError
from .external_evaluation_admission import MAX_EXTERNAL_EVALUATION_DATASET_BYTES
from .external_evaluation_admission import verify_external_evaluation_admission_report
from .filesystem_boundaries import assert_safe_path
from .filesystem_boundaries import read_bytes_safely
from .memory_evaluation import EvaluationCase
from .memory_evaluation import EvaluationManifest
from .memory_evaluation import MAX_SMOKE_CASES
from .memory_evaluation import RetrievalReceipt
from .memory_evaluation import evaluate_retrieval


ROUTE = "bhm-lexical-authoritative.v1"
PROJECT = "longmemeval-local-smoke"
_TYPE_TO_CATEGORY = {
    "single-session-user": "single_hop",
    "single-session-assistant": "assistant_fact",
    "single-session-preference": "preference",
    "temporal-reasoning": "temporal",
    "knowledge-update": "knowledge_update",
    "multi-session": "multi_hop",
}


class LongMemEvalSmokeError(ValueError):
    """Raised when an external smoke input cannot prove its bounded contract."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bytes_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(value: object, field: str, *, limit: int = 160) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise LongMemEvalSmokeError(f"{field} is required and bounded")
    return text


def _load_dataset(path: str | Path) -> tuple[tuple[dict[str, Any], ...], str]:
    dataset_path = assert_safe_path(path).resolve()
    try:
        raw = read_bytes_safely(dataset_path, max_bytes=MAX_EXTERNAL_EVALUATION_DATASET_BYTES)
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LongMemEvalSmokeError("LongMemEval dataset must be bounded UTF-8 JSON") from exc
    if not isinstance(payload, list) or not payload or len(payload) > 500:
        raise LongMemEvalSmokeError("LongMemEval-S dataset must contain 1..500 cases")
    if any(not isinstance(item, dict) for item in payload):
        raise LongMemEvalSmokeError("LongMemEval dataset case must be an object")
    return tuple(payload), _bytes_digest(raw)


def _category(item: Mapping[str, Any]) -> str:
    question_id = _text(item.get("question_id"), "question_id")
    if question_id.endswith("_abs"):
        return "abstention"
    category = _TYPE_TO_CATEGORY.get(str(item.get("question_type") or "").strip())
    if category is None:
        raise LongMemEvalSmokeError("LongMemEval question_type is unsupported")
    return category


def _select_cases(
    items: tuple[dict[str, Any], ...], *, max_cases: int, split_index: int = 0,
) -> tuple[dict[str, Any], ...]:
    if max_cases < 1 or max_cases > MAX_SMOKE_CASES:
        raise LongMemEvalSmokeError(f"max_cases must be between 1 and {MAX_SMOKE_CASES}")
    if not isinstance(split_index, int) or split_index < 0 or split_index > 9:
        raise LongMemEvalSmokeError("split_index must be an integer between 0 and 9")
    buckets: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for item in sorted(items, key=lambda value: _text(value.get("question_id"), "question_id")):
        buckets[_category(item)].append(item)
    selected: list[dict[str, Any]] = []
    categories = tuple(sorted(buckets))
    required = max_cases * (split_index + 1)
    while len(selected) < required and any(buckets.values()):
        for category in categories:
            if buckets[category] and len(selected) < required:
                selected.append(buckets[category].popleft())
    start = max_cases * split_index
    result = tuple(selected[start:required])
    if len(result) != max_cases:
        raise LongMemEvalSmokeError("LongMemEval dataset does not contain a complete requested stratified split")
    return result


def _session_text(session: object) -> str:
    if not isinstance(session, list):
        raise LongMemEvalSmokeError("LongMemEval haystack session must be an array")
    parts: list[str] = []
    for turn in session:
        if not isinstance(turn, dict):
            raise LongMemEvalSmokeError("LongMemEval session turn must be an object")
        content = str(turn.get("content") or "").strip()
        if content:
            parts.append(content)
    result = "\n".join(parts)
    if not result:
        raise LongMemEvalSmokeError("LongMemEval session content must not be empty")
    return result


def _case_records(item: Mapping[str, Any]) -> tuple[tuple[dict[str, Any], ...], dict[str, str]]:
    question_id = _text(item.get("question_id"), "question_id")
    raw_ids = item.get("haystack_session_ids")
    raw_sessions = item.get("haystack_sessions")
    if not isinstance(raw_ids, list) or not isinstance(raw_sessions, list) or len(raw_ids) != len(raw_sessions):
        raise LongMemEvalSmokeError("LongMemEval haystack IDs and sessions must be matching arrays")
    records: list[dict[str, Any]] = []
    source_ids: dict[str, str] = {}
    for raw_id, raw_session in zip(raw_ids, raw_sessions, strict=True):
        session_id = _text(raw_id, "haystack_session_id")
        source_id = f"lme:{question_id}:{session_id}"
        source_ids[session_id] = source_id
        records.append(
            {
                "source_id": source_id,
                "memory": _session_text(raw_session),
                "metadata": {
                    "raw_title": f"LongMemEval session {session_id}",
                    "tags": ["conversation", "evaluation"],
                    "memory_type": "session",
                    "project": PROJECT,
                },
            }
        )
    if not records:
        raise LongMemEvalSmokeError("LongMemEval case must contain haystack sessions")
    return tuple(records), source_ids


def _rank(query: str, records: tuple[dict[str, Any], ...], *, k: int) -> tuple[str, ...]:
    ranked: list[tuple[float, str]] = []
    for record in records:
        score = bhm_app._lexical_signal(query, record)
        score += bhm_app._memory_type_weight(record)
        score += bhm_app._query_intent_weight(query, record)
        if score > 0:
            ranked.append((score, str(record["source_id"])))
    ranked.sort(key=lambda value: (-value[0], value[1]))
    return tuple(source_id for _score, source_id in ranked[:k])


def run_longmemeval_lexical_smoke(
    dataset_path: str | Path,
    *,
    dataset_version: str,
    admission_report: Mapping[str, Any],
    max_cases: int = MAX_SMOKE_CASES,
    k: int = 5,
    split_index: int = 0,
) -> dict[str, Any]:
    """Create one digest-bound LongMemEval-S lexical receipt without mutation."""

    if k < 1 or k > 50:
        raise LongMemEvalSmokeError("k must be between 1 and 50")
    try:
        admission = verify_external_evaluation_admission_report(dict(admission_report))
    except ExternalEvaluationAdmissionError as exc:
        raise LongMemEvalSmokeError("LongMemEval admission report is invalid") from exc
    if admission["dataset"]["suite"] != "longmemeval":
        raise LongMemEvalSmokeError("admission report suite must be longmemeval")
    if admission["dataset"]["version"] != dataset_version:
        raise LongMemEvalSmokeError("admission report version does not match requested dataset")
    items, dataset_digest = _load_dataset(dataset_path)
    if admission["dataset"]["dataset_digest"] != dataset_digest:
        raise LongMemEvalSmokeError("admission report digest does not match local dataset")

    cases: list[EvaluationCase] = []
    receipts: list[RetrievalReceipt] = []
    for item in _select_cases(items, max_cases=max_cases, split_index=split_index):
        question_id = _text(item.get("question_id"), "question_id")
        question = _text(item.get("question"), "question", limit=20_000)
        category = _category(item)
        records, source_ids = _case_records(item)
        answer_ids = item.get("answer_session_ids")
        if not isinstance(answer_ids, list):
            raise LongMemEvalSmokeError("LongMemEval answer_session_ids must be an array")
        expected_ids = tuple(source_ids[session_id] for raw_id in answer_ids if (session_id := _text(raw_id, "answer_session_id")) in source_ids)
        if category != "abstention" and not expected_ids:
            raise LongMemEvalSmokeError("LongMemEval non-abstention case has no answer session in haystack")
        # An abstention question must score only the decision to abstain. Its
        # annotated supporting sessions may explain the label but cannot turn
        # a non-abstaining retrieval into an apparent recall success.
        metric_expected_ids = () if category == "abstention" else expected_ids
        source_digest = _digest(
            {
                "question_id": question_id,
                "question_type": str(item.get("question_type") or ""),
                "haystack_session_ids": tuple(sorted(source_ids)),
                "answer_session_ids": tuple(sorted(metric_expected_ids)),
            }
        )
        started = time.perf_counter()
        retrieved_ids = _rank(question, records, k=k)
        cases.append(
            EvaluationCase(
                case_id=question_id,
                suite="longmemeval",
                category=category,
                expected_ids=metric_expected_ids,
                expected_abstention=category == "abstention",
                project=PROJECT,
                session_id=question_id,
                source_digest=source_digest,
            )
        )
        receipts.append(
            RetrievalReceipt(
                case_id=question_id,
                retrieved_ids=retrieved_ids,
                abstained=not retrieved_ids,
                latency_seconds=time.perf_counter() - started,
                route=ROUTE,
                project=PROJECT,
                provenance_digest=source_digest,
            )
        )
    manifest = EvaluationManifest(
        suite="longmemeval",
        dataset_version=dataset_version,
        dataset_digest=dataset_digest,
        admission_digest=admission["admission_digest"],
        cases=tuple(cases),
        max_model_calls=0,
    )
    report = evaluate_retrieval(manifest, tuple(receipts), k=k, admission_report=admission)
    report["execution"] = {
        "network": False,
        "model_calls": 0,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "route": ROUTE,
        "case_split_index": split_index,
        "runtime_feature_enabled": False,
    }
    report["report_digest"] = _digest({key: value for key, value in report.items() if key != "report_digest"})
    return {
        "manifest": manifest.model_dump(mode="json"),
        "receipts": [item.model_dump(mode="json") for item in receipts],
        "report": report,
    }


__all__ = ["LongMemEvalSmokeError", "PROJECT", "ROUTE", "run_longmemeval_lexical_smoke"]
