"""Offline-only benchmark adapter contract for WL-300.5.

Adapters consume frozen local cases and recorded retrieval receipts.  They do
not fetch datasets, call models, or mutate SQLite/Qdrant/Mem0.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .filesystem_boundaries import assert_safe_path


SCHEMA_VERSION = "bhm.memory-evaluation.v1"
SUPPORTED_SUITES = frozenset({"locomo", "longmemeval", "bhm-fixture"})
MAX_SMOKE_CASES = 50
MAX_BOUNDED_CALLS = 666
FROZEN_FIXTURE_SCHEMA_VERSION = "bhm.memory-evaluation.fixture.v1"
_BHM_FIXTURE_LICENSE = {"name": "0BSD", "source": "BHM-owned"}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=160)
    suite: Literal["locomo", "longmemeval", "bhm-fixture"]
    category: Literal[
        "single_hop", "multi_hop", "temporal", "knowledge_update", "abstention",
        "assistant_fact", "preference", "changing_fact", "implicit_connection", "adversarial",
    ]
    expected_ids: tuple[str, ...] = ()
    expected_abstention: bool = False
    project: str = Field(min_length=1, max_length=160)
    session_id: str = Field(default="unscoped", min_length=1, max_length=160)
    turn_id: str | None = Field(default=None, min_length=1, max_length=160)
    source_digest: str = Field(min_length=64, max_length=64)

    @field_validator("expected_ids", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("expected_ids must be an array")
        return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


class RetrievalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    retrieved_ids: tuple[str, ...] = ()
    abstained: bool = False
    latency_seconds: float = Field(ge=0.0)
    route: str = "local"

    @field_validator("retrieved_ids", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> tuple[str, ...]:
        return EvaluationCase._ids(value)


class EvaluationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = SCHEMA_VERSION
    suite: Literal["locomo", "longmemeval", "bhm-fixture"]
    dataset_version: str = Field(min_length=1, max_length=80)
    dataset_digest: str = Field(min_length=64, max_length=64)
    cases: tuple[EvaluationCase, ...]
    max_model_calls: int = Field(default=MAX_SMOKE_CASES, ge=0, le=MAX_BOUNDED_CALLS)

    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class FrozenEvaluationFixtureError(RuntimeError):
    """Raised when an offline fixture is malformed, altered, or out of scope."""


def load_frozen_evaluation_fixture(path: str | Path) -> dict[str, Any]:
    """Load one BHM-owned, license-bound fixture without network or model use."""

    fixture_path = assert_safe_path(path).resolve()
    if not fixture_path.is_file():
        raise FrozenEvaluationFixtureError("frozen evaluation fixture is missing")
    try:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrozenEvaluationFixtureError("frozen evaluation fixture is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise FrozenEvaluationFixtureError("frozen evaluation fixture root must be an object")
    expected_keys = {
        "fixture_schema_version",
        "license",
        "dataset",
        "dataset_digest",
        "recorded_receipts",
    }
    if set(payload) != expected_keys:
        raise FrozenEvaluationFixtureError("frozen evaluation fixture has unexpected fields")
    if payload.get("fixture_schema_version") != FROZEN_FIXTURE_SCHEMA_VERSION:
        raise FrozenEvaluationFixtureError("unsupported frozen evaluation fixture schema")
    if payload.get("license") != _BHM_FIXTURE_LICENSE:
        raise FrozenEvaluationFixtureError("fixture must be BHM-owned 0BSD content")
    dataset = payload.get("dataset")
    if not isinstance(dataset, dict):
        raise FrozenEvaluationFixtureError("fixture dataset must be an object")
    expected_digest = _digest(dataset)
    if str(payload.get("dataset_digest") or "") != expected_digest:
        raise FrozenEvaluationFixtureError("fixture dataset digest mismatch")
    manifest_payload = {**dataset, "dataset_digest": expected_digest}
    try:
        manifest = EvaluationManifest.model_validate(manifest_payload)
    except ValueError as exc:
        raise FrozenEvaluationFixtureError("fixture dataset contract is invalid") from exc
    if manifest.suite != "bhm-fixture":
        raise FrozenEvaluationFixtureError("only BHM-owned fixtures are admitted locally")
    if any(case.suite != manifest.suite for case in manifest.cases):
        raise FrozenEvaluationFixtureError("fixture case suite must match manifest suite")
    if len(manifest.cases) > MAX_SMOKE_CASES:
        raise FrozenEvaluationFixtureError("frozen fixture exceeds the 50-case smoke limit")
    raw_receipts = payload.get("recorded_receipts")
    if not isinstance(raw_receipts, list):
        raise FrozenEvaluationFixtureError("fixture recorded_receipts must be an array")
    try:
        receipts = tuple(RetrievalReceipt.model_validate(item) for item in raw_receipts)
    except ValueError as exc:
        raise FrozenEvaluationFixtureError("fixture retrieval receipt is invalid") from exc
    expected_case_ids = {case.case_id for case in manifest.cases}
    receipt_ids = [receipt.case_id for receipt in receipts]
    if len(receipt_ids) != len(set(receipt_ids)) or set(receipt_ids) != expected_case_ids:
        raise FrozenEvaluationFixtureError("fixture receipts must cover each case exactly once")
    return {
        "schema_version": FROZEN_FIXTURE_SCHEMA_VERSION,
        "license": dict(_BHM_FIXTURE_LICENSE),
        "manifest": manifest,
        "receipts": receipts,
        "fixture_digest": _digest(payload),
        "execution": {
            "network": False,
            "model_calls": 0,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
        },
    }


def run_frozen_evaluation_fixture(path: str | Path, *, k: int = 5) -> dict[str, Any]:
    """Evaluate a recorded BHM-owned fixture and bind report to exact inputs."""

    fixture = load_frozen_evaluation_fixture(path)
    report = evaluate_retrieval(fixture["manifest"], fixture["receipts"], k=k)
    report["fixture"] = {
        "schema_version": fixture["schema_version"],
        "license": fixture["license"],
        "fixture_digest": fixture["fixture_digest"],
    }
    report["execution"] = dict(fixture["execution"])
    report["report_digest"] = _digest({key: value for key, value in report.items() if key != "report_digest"})
    return report


def _reciprocal_rank(expected: set[str], actual: tuple[str, ...]) -> float:
    for index, candidate in enumerate(actual, start=1):
        if candidate in expected:
            return 1.0 / index
    return 0.0


def _average_precision(expected: set[str], actual: tuple[str, ...]) -> float:
    hits = 0
    precision_sum = 0.0
    for index, candidate in enumerate(actual, start=1):
        if candidate in expected:
            hits += 1
            precision_sum += hits / index
    return precision_sum / len(expected) if expected else 0.0


def _ndcg(expected: set[str], actual: tuple[str, ...]) -> float:
    if not expected:
        return 0.0
    actual_gain = sum(1.0 / math.log2(index + 1) for index, candidate in enumerate(actual, start=1) if candidate in expected)
    ideal_gain = sum(1.0 / math.log2(index + 1) for index in range(1, min(len(expected), len(actual)) + 1))
    return actual_gain / ideal_gain if ideal_gain else 0.0


def _new_metrics() -> dict[str, float]:
    return {"count": 0.0, "recall": 0.0, "precision": 0.0, "mrr": 0.0, "map": 0.0, "ndcg": 0.0, "abstention": 0.0}


def _render_metrics(groups: dict[str, dict[str, float]]) -> dict[str, dict[str, float | int | None]]:
    return {name: {"count": int(values["count"]), "recall_at_k": round(values["recall"] / values["count"], 6) if values["count"] else None, "precision_at_k": round(values["precision"] / values["count"], 6) if values["count"] else None, "mrr": round(values["mrr"] / values["count"], 6) if values["count"] else None, "map_at_k": round(values["map"] / values["count"], 6) if values["count"] else None, "ndcg_at_k": round(values["ndcg"] / values["count"], 6) if values["count"] else None, "abstention_accuracy": round(values["abstention"] / values["count"], 6) if values["count"] else None} for name, values in sorted(groups.items())}


def evaluate_retrieval(manifest: EvaluationManifest, receipts: tuple[RetrievalReceipt, ...], *, k: int = 5) -> dict[str, Any]:
    """Evaluate recorded retrieval only; missing receipts fail closed in report."""

    if k < 1 or k > 50:
        raise ValueError("k must be between 1 and 50")
    receipt_by_case = {receipt.case_id: receipt for receipt in receipts}
    categories: dict[str, dict[str, float]] = defaultdict(_new_metrics)
    sessions: dict[str, dict[str, float]] = defaultdict(_new_metrics)
    turns: dict[str, dict[str, float]] = defaultdict(_new_metrics)
    missing: list[str] = []
    for case in manifest.cases:
        receipt = receipt_by_case.get(case.case_id)
        if receipt is None:
            missing.append(case.case_id)
            continue
        expected = set(case.expected_ids)
        actual = receipt.retrieved_ids[:k]
        hits = len(expected & set(actual))
        group_names = (case.category, case.session_id, f"{case.session_id}/{case.turn_id or case.case_id}")
        for group, name in zip((categories, sessions, turns), group_names, strict=True):
            values = group[name]
            values["count"] += 1
            if expected:
                values["recall"] += hits / len(expected)
                values["precision"] += hits / max(len(actual), 1)
                values["mrr"] += _reciprocal_rank(expected, actual)
                values["map"] += _average_precision(expected, actual)
                values["ndcg"] += _ndcg(expected, actual)
            values["abstention"] += float(receipt.abstained is case.expected_abstention)
    latencies = sorted(receipt.latency_seconds for receipt in receipts)
    p95_index = max(0, min(len(latencies) - 1, int(len(latencies) * 0.95) - 1)) if latencies else None
    report = {
        "schema_version": SCHEMA_VERSION,
        "manifest_digest": manifest.digest(),
        "suite": manifest.suite,
        "k": k,
        "case_count": len(manifest.cases),
        "receipt_count": len(receipts),
        "missing_case_ids": sorted(missing),
        "metrics_by_category": _render_metrics(categories),
        "metrics_by_session": _render_metrics(sessions),
        "metrics_by_turn": _render_metrics(turns),
        "latency_p95_seconds": latencies[p95_index] if p95_index is not None else None,
        "execution": {"network": False, "model_calls": 0, "sqlite_mutation": False, "qdrant_mutation": False},
    }
    report["report_digest"] = _digest(report)
    return report


__all__ = [
    "EvaluationCase",
    "EvaluationManifest",
    "FROZEN_FIXTURE_SCHEMA_VERSION",
    "FrozenEvaluationFixtureError",
    "MAX_BOUNDED_CALLS",
    "MAX_SMOKE_CASES",
    "RetrievalReceipt",
    "SCHEMA_VERSION",
    "SUPPORTED_SUITES",
    "evaluate_retrieval",
    "load_frozen_evaluation_fixture",
    "run_frozen_evaluation_fixture",
]
