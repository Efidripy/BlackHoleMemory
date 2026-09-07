"""Bounded disposable LoCoMo retrieval smoke using BHM lexical scoring.

The adapter consumes only a pinned, already-admitted local dataset.  It emits
content-free manifests, retrieval receipts and metrics, and never starts BHM,
calls a model, retrieves image URLs, or mutates SQLite, Qdrant or Mem0.
"""

from __future__ import annotations

import hashlib
import json
import re
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


ROUTE = "bhm-lexical-disposable.v1"
ADVERSARIAL_ROUTE = "locomo-adversarial-abstain.v1"
PROJECT = "locomo-local-smoke"
UPSTREAM_CATEGORY_MAP = {
    1: "multi_hop",
    2: "temporal",
    3: "open_domain",
    4: "single_hop",
    5: "adversarial",
}
_SESSION_KEY = re.compile(r"session_[1-9][0-9]*$")
_DIALOGUE_ID = re.compile(r"D:?(?P<session>[1-9][0-9]*):(?P<turn>[0-9]+)$")


class LoCoMoSmokeError(ValueError):
    """Raised when the local LoCoMo input cannot prove its bounded contract."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bytes_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(value: object, field: str, *, limit: int = 20_000) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise LoCoMoSmokeError(f"{field} is required and bounded")
    return text


def _rooted_dataset_path(dataset_root: str | Path, dataset_path: str | Path) -> Path:
    root = assert_safe_path(dataset_root).resolve()
    candidate = assert_safe_path(dataset_path).resolve()
    if not root.is_dir():
        raise LoCoMoSmokeError("dataset root must be an existing directory")
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise LoCoMoSmokeError("LoCoMo dataset must stay inside dataset root") from exc
    if not candidate.is_file():
        raise LoCoMoSmokeError("LoCoMo dataset must be an existing regular file")
    return candidate


def _load_dataset(dataset_root: str | Path, dataset_path: str | Path) -> tuple[tuple[dict[str, Any], ...], str]:
    path = _rooted_dataset_path(dataset_root, dataset_path)
    try:
        raw = read_bytes_safely(path, max_bytes=MAX_EXTERNAL_EVALUATION_DATASET_BYTES)
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LoCoMoSmokeError("LoCoMo dataset must be bounded UTF-8 JSON") from exc
    if not isinstance(payload, list) or not payload or len(payload) > 100:
        raise LoCoMoSmokeError("LoCoMo dataset must contain 1..100 samples")
    if any(not isinstance(item, dict) for item in payload):
        raise LoCoMoSmokeError("LoCoMo dataset sample must be an object")
    return tuple(payload), _bytes_digest(raw)


def _sample_id(sample: Mapping[str, Any]) -> str:
    return _text(sample.get("sample_id"), "sample_id", limit=160)


def _category(raw: object) -> tuple[int, str]:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise LoCoMoSmokeError("LoCoMo category must be an integer")
    category = UPSTREAM_CATEGORY_MAP.get(raw)
    if category is None:
        raise LoCoMoSmokeError("LoCoMo category is unsupported")
    return raw, category


def _case_id(sample: Mapping[str, Any], qa_index: int) -> str:
    if qa_index < 0:
        raise LoCoMoSmokeError("LoCoMo QA index must be non-negative")
    return f"locomo:{_sample_id(sample)}:qa:{qa_index}"


def _dialogue_id(value: object, field: str) -> str:
    text = _text(value, field, limit=160)
    match = _DIALOGUE_ID.fullmatch(text)
    if match is None:
        raise LoCoMoSmokeError(f"{field} is unsupported")
    return f"D{int(match.group('session'))}:{int(match.group('turn'))}"


def _select_cases(
    samples: tuple[dict[str, Any], ...], *, eligible_case_ids: set[str], max_cases: int,
) -> tuple[tuple[dict[str, Any], int, dict[str, Any]], ...]:
    if max_cases < 1 or max_cases > MAX_SMOKE_CASES:
        raise LoCoMoSmokeError(f"max_cases must be between 1 and {MAX_SMOKE_CASES}")
    buckets: dict[int, deque[tuple[dict[str, Any], int, dict[str, Any]]]] = defaultdict(deque)
    seen_case_ids: set[str] = set()
    for sample in samples:
        raw_qa = sample.get("qa")
        if not isinstance(raw_qa, list) or not raw_qa:
            raise LoCoMoSmokeError("LoCoMo sample QA must be a non-empty array")
        _sample_id(sample)
        for qa_index, qa in enumerate(raw_qa):
            if not isinstance(qa, dict):
                raise LoCoMoSmokeError("LoCoMo QA must be an object")
            category_number, _category_name = _category(qa.get("category"))
            case_id = _case_id(sample, qa_index)
            if case_id in seen_case_ids:
                raise LoCoMoSmokeError("LoCoMo case IDs must be unique")
            seen_case_ids.add(case_id)
            if case_id in eligible_case_ids:
                buckets[category_number].append((sample, qa_index, qa))
    if set(buckets) != set(UPSTREAM_CATEGORY_MAP):
        raise LoCoMoSmokeError("LoCoMo dataset must contain an admissible case for every supported category")
    for category, items in buckets.items():
        # A simple lexicographic case-ID sort would consume the first sample's
        # full QA block before touching the other nine conversations.  Interleave
        # QA queues by sample first, then round-robin categories below.
        sample_buckets: dict[str, deque[tuple[dict[str, Any], int, dict[str, Any]]]] = defaultdict(deque)
        for item in sorted(items, key=lambda candidate: (_sample_id(candidate[0]), candidate[1])):
            sample_buckets[_sample_id(item[0])].append(item)
        interleaved: list[tuple[dict[str, Any], int, dict[str, Any]]] = []
        while any(sample_buckets.values()):
            for sample_id in sorted(sample_buckets):
                if sample_buckets[sample_id]:
                    interleaved.append(sample_buckets[sample_id].popleft())
        buckets[category] = deque(interleaved)
    selected: list[tuple[dict[str, Any], int, dict[str, Any]]] = []
    while len(selected) < max_cases and any(buckets.values()):
        for category in sorted(UPSTREAM_CATEGORY_MAP):
            if buckets[category] and len(selected) < max_cases:
                selected.append(buckets[category].popleft())
    if len(selected) != max_cases:
        raise LoCoMoSmokeError("LoCoMo dataset does not contain enough admissible cases for the requested split")
    return tuple(selected)


def _records(sample: Mapping[str, Any]) -> tuple[tuple[dict[str, Any], ...], dict[str, str], list[dict[str, str]]]:
    conversation = sample.get("conversation")
    if not isinstance(conversation, dict):
        raise LoCoMoSmokeError("LoCoMo conversation must be an object")
    sample_id = _sample_id(sample)
    records: list[dict[str, Any]] = []
    source_ids: dict[str, str] = {}
    source_digests: list[dict[str, str]] = []
    for session_key, turns in sorted(conversation.items()):
        if not _SESSION_KEY.fullmatch(str(session_key)):
            continue
        if not isinstance(turns, list):
            raise LoCoMoSmokeError("LoCoMo session must be an array")
        for turn in turns:
            if not isinstance(turn, dict):
                raise LoCoMoSmokeError("LoCoMo dialogue turn must be an object")
            dialogue_id = _dialogue_id(turn.get("dia_id"), "LoCoMo dialogue ID")
            content = _text(turn.get("text"), "LoCoMo dialogue text")
            if dialogue_id in source_ids:
                raise LoCoMoSmokeError("LoCoMo dialogue IDs must be unique per sample")
            source_id = f"locomo:{sample_id}:{dialogue_id}"
            source_ids[dialogue_id] = source_id
            source_digests.append({"source_id": source_id, "text_digest": _bytes_digest(content.encode("utf-8"))})
            records.append(
                {
                    "source_id": source_id,
                    "memory": content,
                    "metadata": {
                        "raw_title": f"LoCoMo dialogue {dialogue_id}",
                        "tags": ["conversation", "evaluation"],
                        "memory_type": "session",
                        "project": PROJECT,
                    },
                }
            )
    if not records:
        raise LoCoMoSmokeError("LoCoMo sample must contain dialogue records")
    return tuple(records), source_ids, sorted(source_digests, key=lambda value: value["source_id"])


def _expected_ids(qa: Mapping[str, Any], source_ids: Mapping[str, str]) -> tuple[str, ...]:
    evidence = qa.get("evidence")
    if not isinstance(evidence, list):
        raise LoCoMoSmokeError("LoCoMo evidence must be an array")
    expected: list[str] = []
    for value in evidence:
        raw_evidence = _text(value, "LoCoMo evidence ID", limit=160).replace("(", "").replace(")", "").strip()
        tokens = [item for item in re.split(r"[;\s]+", raw_evidence) if item]
        if not tokens:
            raise LoCoMoSmokeError("LoCoMo evidence reference is unsupported")
        for token in tokens:
            evidence_id = _dialogue_id(token, "LoCoMo evidence reference")
            source_id = source_ids.get(evidence_id)
            if source_id is None:
                raise LoCoMoSmokeError("LoCoMo evidence reference is unsupported")
            if source_id not in expected:
                expected.append(source_id)
    return tuple(expected)


def _eligible_case_ids(samples: tuple[dict[str, Any], ...]) -> tuple[set[str], dict[str, int]]:
    """Reject unsupported retrieval rows before sampling.

    The released corpus has a small number of annotation rows whose evidence
    is not a valid dialogue reference, as well as open-domain rows with no
    evidence at all. They are never silently treated as a retrieval case with
    invented expected IDs: the deterministic smoke excludes them and records
    distinct aggregate reasons. All other malformed source structure raises.
    """

    eligible: set[str] = set()
    exclusions = {"unresolvable_evidence": 0, "missing_retrieval_evidence": 0}
    for sample in samples:
        records, source_ids, _source_digests = _records(sample)
        if not records:
            raise LoCoMoSmokeError("LoCoMo sample must contain dialogue records")
        raw_qa = sample.get("qa")
        if not isinstance(raw_qa, list) or not raw_qa:
            raise LoCoMoSmokeError("LoCoMo sample QA must be a non-empty array")
        for qa_index, qa in enumerate(raw_qa):
            if not isinstance(qa, dict):
                raise LoCoMoSmokeError("LoCoMo QA must be an object")
            _category_number, category = _category(qa.get("category"))
            _text(qa.get("question"), "LoCoMo question")
            try:
                expected_ids = _expected_ids(qa, source_ids)
            except LoCoMoSmokeError as exc:
                if str(exc) != "LoCoMo evidence reference is unsupported":
                    raise
                exclusions["unresolvable_evidence"] += 1
                continue
            if category != "adversarial" and not expected_ids:
                exclusions["missing_retrieval_evidence"] += 1
                continue
            eligible.add(_case_id(sample, qa_index))
    return eligible, exclusions


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


def run_locomo_lexical_smoke(
    dataset_root: str | Path,
    dataset_path: str | Path,
    *,
    dataset_version: str,
    admission_report: Mapping[str, Any],
    max_cases: int = MAX_SMOKE_CASES,
    k: int = 5,
) -> dict[str, Any]:
    """Create one digest-bound local LoCoMo receipt without persistent effects."""

    if k < 1 or k > 50:
        raise LoCoMoSmokeError("k must be between 1 and 50")
    try:
        admission = verify_external_evaluation_admission_report(dict(admission_report))
    except ExternalEvaluationAdmissionError as exc:
        raise LoCoMoSmokeError("LoCoMo admission report is invalid") from exc
    if admission["dataset"]["suite"] != "locomo":
        raise LoCoMoSmokeError("admission report suite must be locomo")
    if admission["dataset"]["version"] != dataset_version:
        raise LoCoMoSmokeError("admission report version does not match requested dataset")
    samples, dataset_digest = _load_dataset(dataset_root, dataset_path)
    if admission["dataset"]["dataset_digest"] != dataset_digest:
        raise LoCoMoSmokeError("admission report digest does not match local dataset")

    eligible_case_ids, exclusion_counts = _eligible_case_ids(samples)
    cases: list[EvaluationCase] = []
    receipts: list[RetrievalReceipt] = []
    for sample, qa_index, qa in _select_cases(samples, eligible_case_ids=eligible_case_ids, max_cases=max_cases):
        case_id = _case_id(sample, qa_index)
        _category_number, category = _category(qa.get("category"))
        question = _text(qa.get("question"), "LoCoMo question")
        records, source_ids, source_digests = _records(sample)
        expected_ids = _expected_ids(qa, source_ids)
        expected_abstention = category == "adversarial"
        metric_expected_ids = () if expected_abstention else expected_ids
        source_digest = _digest(
            {
                "case_id": case_id,
                "category": category,
                "question_digest": _bytes_digest(question.encode("utf-8")),
                "expected_ids": metric_expected_ids,
                "sources": source_digests,
            }
        )
        started = time.perf_counter()
        if expected_abstention:
            retrieved_ids = ()
            abstained = True
            route = ADVERSARIAL_ROUTE
        else:
            retrieved_ids = _rank(question, records, k=k)
            abstained = not retrieved_ids
            route = ROUTE
        cases.append(
            EvaluationCase(
                case_id=case_id,
                suite="locomo",
                category=category,
                expected_ids=metric_expected_ids,
                expected_abstention=expected_abstention,
                project=PROJECT,
                session_id=_sample_id(sample),
                source_digest=source_digest,
            )
        )
        receipts.append(
            RetrievalReceipt(
                case_id=case_id,
                retrieved_ids=retrieved_ids,
                abstained=abstained,
                latency_seconds=time.perf_counter() - started,
                route=route,
                project=PROJECT,
                provenance_digest=source_digest,
            )
        )
    manifest = EvaluationManifest(
        suite="locomo",
        dataset_version=dataset_version,
        dataset_digest=dataset_digest,
        admission_digest=admission["admission_digest"],
        cases=tuple(cases),
        max_model_calls=0,
    )
    report = evaluate_retrieval(manifest, tuple(receipts), k=k, admission_report=admission)
    report["execution"] = {
        "network": False,
        "dataset_content_emitted": False,
        "image_url_retrieval": False,
        "model_calls": 0,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "runtime_feature_enabled": False,
        "source_evidence_exclusion_count": sum(exclusion_counts.values()),
        "source_missing_retrieval_evidence_exclusion_count": exclusion_counts["missing_retrieval_evidence"],
        "source_unresolvable_evidence_exclusion_count": exclusion_counts["unresolvable_evidence"],
        "route": ROUTE,
        "adversarial_route": ADVERSARIAL_ROUTE,
    }
    report["report_digest"] = _digest({key: value for key, value in report.items() if key != "report_digest"})
    return {
        "manifest": manifest.model_dump(mode="json"),
        "receipts": [receipt.model_dump(mode="json") for receipt in receipts],
        "report": report,
    }


__all__ = [
    "ADVERSARIAL_ROUTE",
    "LoCoMoSmokeError",
    "PROJECT",
    "ROUTE",
    "UPSTREAM_CATEGORY_MAP",
    "run_locomo_lexical_smoke",
]
