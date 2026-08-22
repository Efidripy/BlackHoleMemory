"""Offline-only benchmark adapter contract for WL-300.5.

Adapters consume frozen local cases and recorded retrieval receipts.  They do
not fetch datasets, call models, or mutate SQLite/Qdrant/Mem0.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SCHEMA_VERSION = "bhm.memory-evaluation.v1"
SUPPORTED_SUITES = frozenset({"locomo", "longmemeval", "bhm-fixture"})
MAX_SMOKE_CASES = 50
MAX_BOUNDED_CALLS = 666


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


def _reciprocal_rank(expected: set[str], actual: tuple[str, ...]) -> float:
    for index, candidate in enumerate(actual, start=1):
        if candidate in expected:
            return 1.0 / index
    return 0.0


def evaluate_retrieval(manifest: EvaluationManifest, receipts: tuple[RetrievalReceipt, ...], *, k: int = 5) -> dict[str, Any]:
    """Evaluate recorded retrieval only; missing receipts fail closed in report."""

    if k < 1 or k > 50:
        raise ValueError("k must be between 1 and 50")
    receipt_by_case = {receipt.case_id: receipt for receipt in receipts}
    categories: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0.0, "recall": 0.0, "precision": 0.0, "mrr": 0.0, "abstention": 0.0})
    missing: list[str] = []
    for case in manifest.cases:
        receipt = receipt_by_case.get(case.case_id)
        if receipt is None:
            missing.append(case.case_id)
            continue
        expected = set(case.expected_ids)
        actual = receipt.retrieved_ids[:k]
        hits = len(expected & set(actual))
        category = categories[case.category]
        category["count"] += 1
        if expected:
            category["recall"] += hits / len(expected)
            category["precision"] += hits / max(len(actual), 1)
            category["mrr"] += _reciprocal_rank(expected, actual)
        category["abstention"] += float(receipt.abstained is case.expected_abstention)
    metrics = {
        name: {
            "count": int(values["count"]),
            "recall_at_k": round(values["recall"] / values["count"], 6) if values["count"] else None,
            "precision_at_k": round(values["precision"] / values["count"], 6) if values["count"] else None,
            "mrr": round(values["mrr"] / values["count"], 6) if values["count"] else None,
            "abstention_accuracy": round(values["abstention"] / values["count"], 6) if values["count"] else None,
        }
        for name, values in sorted(categories.items())
    }
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
        "metrics_by_category": metrics,
        "latency_p95_seconds": latencies[p95_index] if p95_index is not None else None,
        "execution": {"network": False, "model_calls": 0, "sqlite_mutation": False, "qdrant_mutation": False},
    }
    report["report_digest"] = _digest(report)
    return report


__all__ = ["EvaluationCase", "EvaluationManifest", "MAX_BOUNDED_CALLS", "MAX_SMOKE_CASES", "RetrievalReceipt", "SCHEMA_VERSION", "SUPPORTED_SUITES", "evaluate_retrieval"]
