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
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .external_evaluation_admission import ExternalEvaluationAdmissionError
from .external_evaluation_admission import verify_external_evaluation_admission_report
from .filesystem_boundaries import assert_safe_path
from .filesystem_boundaries import read_bytes_safely


SCHEMA_VERSION = "bhm.memory-evaluation.v1"
SUPPORTED_SUITES = frozenset({"locomo", "longmemeval", "bhm-fixture"})
MAX_SMOKE_CASES = 50
MAX_BOUNDED_CALLS = 666
FROZEN_FIXTURE_SCHEMA_VERSION = "bhm.memory-evaluation.fixture.v1"
_BHM_FIXTURE_LICENSE = {"name": "0BSD", "source": "BHM-owned"}
_MAX_RECORDED_INPUT_BYTES = 256 * 1024
_EXTERNAL_SUITES = frozenset({"locomo", "longmemeval"})


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
    project: str | None = Field(default=None, min_length=1, max_length=160)
    provenance_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

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
    admission_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _require_suite_bound_admission(self) -> "EvaluationManifest":
        if self.suite in _EXTERNAL_SUITES and self.admission_digest is None:
            raise ValueError("external evaluation manifests require an admission_digest")
        if self.suite == "bhm-fixture" and self.admission_digest is not None:
            raise ValueError("BHM-owned fixture manifests must not carry an external admission_digest")
        if any(case.suite != self.suite for case in self.cases):
            raise ValueError("evaluation case suite must match manifest suite")
        return self

    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class FrozenEvaluationFixtureError(RuntimeError):
    """Raised when an offline fixture is malformed, altered, or out of scope."""


class EvaluationAdmissionBindingError(ValueError):
    """Raised when an external evaluation lacks a matching approved receipt."""


def _load_bounded_json(path: str | Path, *, label: str) -> Any:
    input_path = assert_safe_path(path).resolve()
    if not input_path.is_file():
        raise FrozenEvaluationFixtureError(f"{label} is missing")
    try:
        return json.loads(read_bytes_safely(input_path, max_bytes=_MAX_RECORDED_INPUT_BYTES).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrozenEvaluationFixtureError(f"{label} must be bounded UTF-8 JSON") from exc


def load_recorded_evaluation_manifest(path: str | Path) -> EvaluationManifest:
    """Load one content-free recorded manifest; no dataset content is parsed."""

    payload = _load_bounded_json(path, label="recorded evaluation manifest")
    if not isinstance(payload, dict):
        raise FrozenEvaluationFixtureError("recorded evaluation manifest root must be an object")
    try:
        return EvaluationManifest.model_validate(payload)
    except ValueError as exc:
        raise FrozenEvaluationFixtureError("recorded evaluation manifest contract is invalid") from exc


def load_recorded_retrieval_receipts(path: str | Path) -> tuple[RetrievalReceipt, ...]:
    """Load bounded recorded IDs/metrics only, never prompts or dataset content."""

    payload = _load_bounded_json(path, label="recorded retrieval receipts")
    if not isinstance(payload, list) or len(payload) > MAX_SMOKE_CASES:
        raise FrozenEvaluationFixtureError("recorded retrieval receipts must be a bounded array")
    try:
        return tuple(RetrievalReceipt.model_validate(item) for item in payload)
    except ValueError as exc:
        raise FrozenEvaluationFixtureError("recorded retrieval receipt is invalid") from exc


def verify_evaluation_admission_binding(
    manifest: EvaluationManifest,
    admission_report: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Bind an external replay to its exact local-only admission evidence."""

    if manifest.suite == "bhm-fixture":
        if admission_report is not None:
            raise EvaluationAdmissionBindingError("BHM-owned fixture must not use an external admission report")
        return None
    if admission_report is None:
        raise EvaluationAdmissionBindingError("external evaluation requires a matching admission report")
    try:
        verified = verify_external_evaluation_admission_report(dict(admission_report))
    except ExternalEvaluationAdmissionError as exc:
        raise EvaluationAdmissionBindingError("external evaluation admission report is invalid") from exc
    dataset = verified["dataset"]
    if (
        verified["admission_digest"] != manifest.admission_digest
        or dataset["suite"] != manifest.suite
        or dataset["version"] != manifest.dataset_version
        or dataset["dataset_digest"] != manifest.dataset_digest
    ):
        raise EvaluationAdmissionBindingError("external evaluation admission does not match manifest")
    return {
        "admission_digest": verified["admission_digest"],
        "suite": dataset["suite"],
        "dataset_version": dataset["version"],
        "dataset_digest": dataset["dataset_digest"],
    }


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
    required_keys = {
        "fixture_schema_version",
        "license",
        "dataset",
        "dataset_digest",
        "recorded_receipts",
    }
    optional_keys = {"baseline_receipts"}
    if not required_keys.issubset(payload) or set(payload) - required_keys - optional_keys:
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
    if dataset.get("suite") != "bhm-fixture":
        raise FrozenEvaluationFixtureError("only BHM-owned fixtures are admitted locally")
    raw_cases = dataset.get("cases")
    if not isinstance(raw_cases, (list, tuple)) or any(
        not isinstance(case, dict) or case.get("suite") != "bhm-fixture" for case in raw_cases
    ):
        raise FrozenEvaluationFixtureError("fixture case suite must match manifest suite")
    manifest_payload = {**dataset, "dataset_digest": expected_digest}
    try:
        manifest = EvaluationManifest.model_validate(manifest_payload)
    except ValueError as exc:
        raise FrozenEvaluationFixtureError("fixture dataset contract is invalid") from exc
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
    raw_baseline = payload.get("baseline_receipts")
    baseline_receipts: tuple[RetrievalReceipt, ...] | None = None
    if raw_baseline is not None:
        if not isinstance(raw_baseline, list):
            raise FrozenEvaluationFixtureError("fixture baseline_receipts must be an array")
        try:
            baseline_receipts = tuple(RetrievalReceipt.model_validate(item) for item in raw_baseline)
        except ValueError as exc:
            raise FrozenEvaluationFixtureError("fixture baseline receipt is invalid") from exc
        baseline_ids = [receipt.case_id for receipt in baseline_receipts]
        if len(baseline_ids) != len(set(baseline_ids)) or set(baseline_ids) != expected_case_ids:
            raise FrozenEvaluationFixtureError("fixture baseline receipts must cover each case exactly once")
    return {
        "schema_version": FROZEN_FIXTURE_SCHEMA_VERSION,
        "license": dict(_BHM_FIXTURE_LICENSE),
        "manifest": manifest,
        "receipts": receipts,
        "baseline_receipts": baseline_receipts,
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
    baseline = fixture["baseline_receipts"]
    if baseline is not None:
        report["full_context_baseline"] = compare_full_context_baseline(fixture["manifest"], fixture["receipts"], baseline, k=k)
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


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    index = max(0, min(len(values) - 1, math.ceil(len(values) * quantile) - 1))
    return values[index]


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _numeric_delta(value: float | int | None, baseline: float | int | None) -> float | None:
    if value is None or baseline is None:
        return None
    return round(float(value) - float(baseline), 6)


def _metric_delta(current: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Produce content-free, deterministic metric deltas for one baseline."""

    result: dict[str, Any] = {}
    for name in ("metrics_by_category", "metrics_by_session", "metrics_by_turn", "metrics_by_route"):
        current_groups = current[name]
        baseline_groups = baseline[name]
        result[name] = {
            group: {
                metric: _numeric_delta(current_values.get(metric), baseline_values.get(metric))
                for metric in ("recall_at_k", "precision_at_k", "mrr", "map_at_k", "ndcg_at_k", "abstention_accuracy")
            }
            for group, current_values in current_groups.items()
            if (baseline_values := baseline_groups.get(group)) is not None
        }
    current_capabilities = current["capability_metrics"]
    baseline_capabilities = baseline["capability_metrics"]
    result["capability_metrics"] = {
        "temporal_accuracy": _numeric_delta(current_capabilities["temporal_accuracy"]["accuracy"], baseline_capabilities["temporal_accuracy"]["accuracy"]),
        "update_consistency": _numeric_delta(current_capabilities["update_consistency"]["accuracy"], baseline_capabilities["update_consistency"]["accuracy"]),
        "abstention_precision": _numeric_delta(current_capabilities["abstention"]["precision"], baseline_capabilities["abstention"]["precision"]),
        "abstention_recall": _numeric_delta(current_capabilities["abstention"]["recall"], baseline_capabilities["abstention"]["recall"]),
    }
    result["latency_p50_seconds"] = _numeric_delta(current["latency_p50_seconds"], baseline["latency_p50_seconds"])
    result["latency_p95_seconds"] = _numeric_delta(current["latency_p95_seconds"], baseline["latency_p95_seconds"])
    return result


def compare_full_context_baseline(
    manifest: EvaluationManifest,
    receipts: tuple[RetrievalReceipt, ...],
    baseline_receipts: tuple[RetrievalReceipt, ...],
    *,
    k: int = 5,
) -> dict[str, Any]:
    """Compare recorded retrieval to a complete recorded small-context baseline.

    Both lanes are already-recorded IDs and metrics.  This is an evidence
    comparator, never a query planner or a path that can enable runtime policy.
    """

    current = evaluate_retrieval(manifest, receipts, k=k)
    baseline = evaluate_retrieval(manifest, baseline_receipts, k=k)
    if not baseline["input_integrity"]["valid"] or baseline["scored_receipt_count"] != len(manifest.cases):
        raise FrozenEvaluationFixtureError("full-context baseline must have complete unambiguous coverage")
    if baseline["provenance_and_isolation"]["passed"] is not True:
        raise FrozenEvaluationFixtureError("full-context baseline must prove project and provenance isolation")
    return {
        "policy": "recorded-full-context.v1",
        "case_count": len(manifest.cases),
        "baseline_report_digest": baseline["report_digest"],
        "baseline_input_integrity": {
            "valid": True,
            "scored_receipt_count": baseline["scored_receipt_count"],
        },
        "baseline_provenance_and_isolation": {
            "coverage": baseline["provenance_and_isolation"]["coverage"],
            "passed": True,
        },
        "baseline_metrics": {
            "metrics_by_category": baseline["metrics_by_category"],
            "capability_metrics": baseline["capability_metrics"],
            "latency_p50_seconds": baseline["latency_p50_seconds"],
            "latency_p95_seconds": baseline["latency_p95_seconds"],
        },
        "delta_retrieval_minus_full_context": _metric_delta(current, baseline),
        "execution": {"network": False, "model_calls": 0, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False},
    }


def evaluate_retrieval(
    manifest: EvaluationManifest,
    receipts: tuple[RetrievalReceipt, ...],
    *,
    k: int = 5,
    admission_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate recorded retrieval only; missing receipts fail closed in report."""

    if k < 1 or k > 50:
        raise ValueError("k must be between 1 and 50")
    admission = verify_evaluation_admission_binding(manifest, admission_report)
    expected_case_ids = {case.case_id for case in manifest.cases}
    receipt_candidates: dict[str, list[RetrievalReceipt]] = defaultdict(list)
    for receipt in receipts:
        receipt_candidates[receipt.case_id].append(receipt)
    duplicate_receipt_case_ids = sorted(case_id for case_id, values in receipt_candidates.items() if len(values) > 1)
    unknown_receipt_case_ids = sorted(case_id for case_id in receipt_candidates if case_id not in expected_case_ids)
    receipt_by_case = {
        case_id: values[0]
        for case_id, values in receipt_candidates.items()
        if case_id in expected_case_ids and len(values) == 1
    }
    categories: dict[str, dict[str, float]] = defaultdict(_new_metrics)
    sessions: dict[str, dict[str, float]] = defaultdict(_new_metrics)
    turns: dict[str, dict[str, float]] = defaultdict(_new_metrics)
    routes: dict[str, dict[str, float]] = defaultdict(_new_metrics)
    missing: list[str] = []
    temporal_total = temporal_correct = 0
    update_total = update_correct = 0
    expected_abstentions = predicted_abstentions = correct_abstentions = 0
    project_leakage_case_ids: list[str] = []
    provenance_unproven_case_ids: list[str] = []
    provenance_mismatch_case_ids: list[str] = []
    for case in manifest.cases:
        receipt = receipt_by_case.get(case.case_id)
        if receipt is None:
            missing.append(case.case_id)
            continue
        expected = set(case.expected_ids)
        actual = receipt.retrieved_ids[:k]
        actual_set = set(actual)
        hits = len(expected & actual_set)
        fully_correct = bool(expected) and expected.issubset(actual_set)
        group_names = (case.category, case.session_id, f"{case.session_id}/{case.turn_id or case.case_id}", receipt.route)
        for group, name in zip((categories, sessions, turns, routes), group_names, strict=True):
            values = group[name]
            values["count"] += 1
            if expected:
                values["recall"] += hits / len(expected)
                values["precision"] += hits / max(len(actual), 1)
                values["mrr"] += _reciprocal_rank(expected, actual)
                values["map"] += _average_precision(expected, actual)
                values["ndcg"] += _ndcg(expected, actual)
            values["abstention"] += float(receipt.abstained is case.expected_abstention)
        if case.category in {"temporal", "changing_fact"}:
            temporal_total += 1
            temporal_correct += int(fully_correct)
        if case.category in {"knowledge_update", "changing_fact"}:
            update_total += 1
            update_correct += int(fully_correct)
        if case.expected_abstention:
            expected_abstentions += 1
            correct_abstentions += int(receipt.abstained)
        if receipt.abstained:
            predicted_abstentions += 1
        if receipt.project is None or receipt.provenance_digest is None:
            provenance_unproven_case_ids.append(case.case_id)
        else:
            if receipt.project != case.project:
                project_leakage_case_ids.append(case.case_id)
            if receipt.provenance_digest != case.source_digest:
                provenance_mismatch_case_ids.append(case.case_id)
    latencies = sorted(receipt.latency_seconds for receipt in receipt_by_case.values())
    provenance_evaluated_count = len(manifest.cases) - len(missing) - len(provenance_unproven_case_ids)
    isolation_passed: bool | None = None
    if provenance_evaluated_count == len(manifest.cases):
        isolation_passed = not project_leakage_case_ids and not provenance_mismatch_case_ids
    report = {
        "schema_version": SCHEMA_VERSION,
        "manifest_digest": manifest.digest(),
        "suite": manifest.suite,
        "k": k,
        "case_count": len(manifest.cases),
        "receipt_count": len(receipts),
        "scored_receipt_count": len(receipt_by_case),
        "missing_case_ids": sorted(missing),
        "input_integrity": {
            "duplicate_receipt_case_ids": duplicate_receipt_case_ids,
            "unknown_receipt_case_ids": unknown_receipt_case_ids,
            "valid": not missing and not duplicate_receipt_case_ids and not unknown_receipt_case_ids,
        },
        "metrics_by_category": _render_metrics(categories),
        "metrics_by_session": _render_metrics(sessions),
        "metrics_by_turn": _render_metrics(turns),
        "metrics_by_route": _render_metrics(routes),
        "capability_metrics": {
            "temporal_accuracy": {"case_count": temporal_total, "correct_count": temporal_correct, "accuracy": _ratio(temporal_correct, temporal_total)},
            "update_consistency": {"case_count": update_total, "correct_count": update_correct, "accuracy": _ratio(update_correct, update_total)},
            "abstention": {
                "expected_count": expected_abstentions,
                "predicted_count": predicted_abstentions,
                "correct_count": correct_abstentions,
                "precision": _ratio(correct_abstentions, predicted_abstentions),
                "recall": _ratio(correct_abstentions, expected_abstentions),
            },
        },
        "provenance_and_isolation": {
            "evaluated_case_count": provenance_evaluated_count,
            "coverage": _ratio(provenance_evaluated_count, len(manifest.cases)),
            "project_leakage_case_ids": sorted(project_leakage_case_ids),
            "provenance_mismatch_case_ids": sorted(provenance_mismatch_case_ids),
            "unproven_case_ids": sorted(provenance_unproven_case_ids),
            "passed": isolation_passed,
        },
        "latency_p50_seconds": _percentile(latencies, 0.50),
        "latency_p95_seconds": _percentile(latencies, 0.95),
        "execution": {"network": False, "model_calls": 0, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False},
    }
    if admission is not None:
        report["admission"] = admission
    report["report_digest"] = _digest(report)
    return report


__all__ = [
    "EvaluationCase",
    "EvaluationAdmissionBindingError",
    "EvaluationManifest",
    "FROZEN_FIXTURE_SCHEMA_VERSION",
    "FrozenEvaluationFixtureError",
    "MAX_BOUNDED_CALLS",
    "MAX_SMOKE_CASES",
    "RetrievalReceipt",
    "SCHEMA_VERSION",
    "SUPPORTED_SUITES",
    "evaluate_retrieval",
    "compare_full_context_baseline",
    "load_frozen_evaluation_fixture",
    "load_recorded_evaluation_manifest",
    "load_recorded_retrieval_receipts",
    "run_frozen_evaluation_fixture",
    "verify_evaluation_admission_binding",
]
