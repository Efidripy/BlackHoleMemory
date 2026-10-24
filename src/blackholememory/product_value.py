"""Deterministic product-value benchmark and pruning decision for WI-17."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


PRODUCT_VALUE_SCHEMA_VERSION = "bhm.product-value.v1"
SCENARIOS = (
    "find_relevant_file",
    "explain_symbol_callers_and_tests",
    "assess_api_impact",
    "recover_previous_decision",
    "select_tests_after_change",
    "continue_across_codex_claude",
    "diagnose_incident",
    "start_project_without_noise",
    "export_architecture_view",
    "operate_offline_local_qwen",
    "verify_release_and_rollback",
    "review_security_boundary",
)
METRIC_DIRECTIONS = {
    "time_to_relevant_file_ms": "down",
    "tokens_to_model": "down",
    "precision_at_5": "up",
    "unsupported_claim_rate": "down",
    "test_selection_usefulness": "up",
    "duplicate_memory_rate": "down",
    "stale_fact_exposure": "down",
    "human_review_minutes": "down",
    "index_freshness_seconds": "down",
    "context_compile_latency_ms": "down",
    "recovery_success_rate": "up",
    "cross_agent_continuity": "up",
}
METRIC_WEIGHTS = {
    "time_to_relevant_file_ms": 1.0,
    "tokens_to_model": 0.8,
    "precision_at_5": 1.0,
    "unsupported_claim_rate": 1.0,
    "test_selection_usefulness": 0.9,
    "duplicate_memory_rate": 0.6,
    "stale_fact_exposure": 0.8,
    "human_review_minutes": 0.6,
    "index_freshness_seconds": 0.5,
    "context_compile_latency_ms": 0.8,
    "recovery_success_rate": 0.8,
    "cross_agent_continuity": 0.8,
}
DEFAULT_BASELINE = {
    "time_to_relevant_file_ms": 420.0,
    "tokens_to_model": 12_000.0,
    "precision_at_5": 0.55,
    "unsupported_claim_rate": 0.18,
    "test_selection_usefulness": 0.42,
    "duplicate_memory_rate": 0.12,
    "stale_fact_exposure": 0.20,
    "human_review_minutes": 18.0,
    "index_freshness_seconds": 600.0,
    "context_compile_latency_ms": 80.0,
    "recovery_success_rate": 0.60,
    "cross_agent_continuity": 0.45,
}
DEFAULT_INTEGRATED = {
    "time_to_relevant_file_ms": 120.0,
    "tokens_to_model": 7_000.0,
    "precision_at_5": 0.82,
    "unsupported_claim_rate": 0.05,
    "test_selection_usefulness": 0.74,
    "duplicate_memory_rate": 0.04,
    "stale_fact_exposure": 0.06,
    "human_review_minutes": 11.0,
    "index_freshness_seconds": 60.0,
    "context_compile_latency_ms": 12.0,
    "recovery_success_rate": 0.95,
    "cross_agent_continuity": 0.88,
}


class ProductValueError(ValueError):
    """Raised when a benchmark input violates the bounded contract."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _metric_delta(baseline: float, integrated: float, direction: str) -> float:
    denominator = max(abs(float(baseline)), 1.0)
    raw = (float(baseline) - float(integrated)) / denominator if direction == "down" else (float(integrated) - float(baseline)) / denominator
    return round(raw, 6)


def _validate_metrics(values: Mapping[str, Any], name: str) -> dict[str, float]:
    result: dict[str, float] = {}
    missing = [key for key in METRIC_DIRECTIONS if key not in values]
    if missing:
        raise ProductValueError(f"{name} is missing metrics: {', '.join(missing)}")
    for key in METRIC_DIRECTIONS:
        try:
            value = float(values[key])
        except (TypeError, ValueError) as exc:
            raise ProductValueError(f"{name}.{key} must be numeric") from exc
        if value < 0:
            raise ProductValueError(f"{name}.{key} cannot be negative")
        result[key] = value
    return result


def build_product_value_benchmark(
    *,
    baseline: Mapping[str, Any] | None = None,
    integrated: Mapping[str, Any] | None = None,
    scenario_outcomes: Sequence[Mapping[str, Any]] | None = None,
    iterations: int = 16,
    workload: str = "wi17-synthetic-current-bhm-v1",
    safety_violations_baseline: int = 0,
    safety_violations_integrated: int = 0,
    has_deterministic_fallback: bool = True,
    has_second_authority: bool = False,
    optional_features: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if not 1 <= int(iterations) <= 256:
        raise ProductValueError("iterations must be between 1 and 256")
    baseline_values = _validate_metrics(baseline or DEFAULT_BASELINE, "baseline")
    integrated_values = _validate_metrics(integrated or DEFAULT_INTEGRATED, "integrated")
    outcomes = list(scenario_outcomes or [{"scenario": name, "baseline": "manual/current", "integrated": "bounded/evidence"} for name in SCENARIOS])
    if len(outcomes) != len(SCENARIOS):
        raise ProductValueError(f"scenario_outcomes must contain exactly {len(SCENARIOS)} rows")
    scenario_rows = []
    for index, row in enumerate(outcomes):
        scenario = str(row.get("scenario") or SCENARIOS[index])
        if scenario != SCENARIOS[index]:
            raise ProductValueError(f"scenario order mismatch at index {index}: {scenario}")
        scenario_rows.append({"index": index, "scenario": scenario, "baseline": str(row.get("baseline") or "manual/current")[:160], "integrated": str(row.get("integrated") or "bounded/evidence")[:160], "evidence": str(row.get("evidence") or "synthetic fixture")[:200]})
    metric_rows = []
    weighted_sum = 0.0
    weight_sum = 0.0
    for key, direction in METRIC_DIRECTIONS.items():
        delta = _metric_delta(baseline_values[key], integrated_values[key], direction)
        weight = METRIC_WEIGHTS[key]
        weighted_sum += max(delta, 0.0) * weight
        weight_sum += weight
        metric_rows.append({"metric": key, "direction": direction, "baseline": baseline_values[key], "integrated": integrated_values[key], "delta_fraction": delta, "weight": weight, "improved": delta >= 0.0})
    utility_score = round(weighted_sum / max(weight_sum, 1.0), 6)
    negative_metrics = [row["metric"] for row in metric_rows if not row["improved"]]
    safety_non_regression = int(safety_violations_integrated) <= int(safety_violations_baseline)
    pruning = list(optional_features or [
        {"feature": "obsidian_bridge", "decision": "retain-disabled", "reason": "review-only optional bridge; not authoritative"},
        {"feature": "autonomous_apply", "decision": "prune-disabled", "reason": "no separate proven gate; proposal-only invariant"},
        {"feature": "training_lora_qlora", "decision": "prune-disabled", "reason": "explicitly outside current authority and rollback scope"},
        {"feature": "deep_security_scan", "decision": "defer", "reason": "six-worker delegated capacity unavailable in current session"},
    ])
    core = {"workload": str(workload)[:160], "iterations": int(iterations), "scenarios": scenario_rows, "metrics": metric_rows, "utility_score": utility_score, "safety": {"baseline_violations": int(safety_violations_baseline), "integrated_violations": int(safety_violations_integrated)}, "pruning": pruning}
    checks = {
        "scenario_coverage": [row["scenario"] for row in scenario_rows] == list(SCENARIOS),
        "metric_directions_declared": len(metric_rows) == len(METRIC_DIRECTIONS),
        "net_value_positive": utility_score > 0.0 and not negative_metrics,
        "safety_non_regression": safety_non_regression,
        "deterministic_fallback": bool(has_deterministic_fallback),
        "single_authority": not bool(has_second_authority),
        "bounded_iterations": 1 <= int(iterations) <= 256,
        "pruning_recorded": bool(pruning),
    }
    return {"schema_version": PRODUCT_VALUE_SCHEMA_VERSION, "benchmark_digest": _sha256(core), **core, "checks": checks, "decision": "ship-current-scope" if all(checks.values()) else "narrow-or-prune", "evidence_class": "synthetic-bounded-fixture", "real_user_telemetry": False, "execution": {"model_called": False, "agent_started": False, "network_called": False, "sqlite_written": False, "qdrant_written": False, "mem0_written": False, "files_written": False, "apply_performed": False}}


def verify_product_value_digest(report: Mapping[str, Any]) -> bool:
    expected = str(report.get("benchmark_digest") or "")
    if not expected:
        return False
    keys = ("workload", "iterations", "scenarios", "metrics", "utility_score", "safety", "pruning")
    return expected == _sha256({key: report.get(key) for key in keys})


__all__ = [
    "DEFAULT_BASELINE",
    "DEFAULT_INTEGRATED",
    "METRIC_DIRECTIONS",
    "PRODUCT_VALUE_SCHEMA_VERSION",
    "ProductValueError",
    "SCENARIOS",
    "build_product_value_benchmark",
    "verify_product_value_digest",
]
