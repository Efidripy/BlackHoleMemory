"""Readiness threshold policy, stratified metrics derivation, and report gating."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .semantic_assurance_evaluation import (
    recompute_semantic_assurance_results,
    wilson_interval,
)
from .semantic_assurance_live import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_VARIANT,
    PROMPT_CONTRACT,
)

LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V5 = "bhm.semantic-assurance-human-label-admission-receipt.v5"
LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V6 = "bhm.semantic-assurance-human-label-admission-receipt.v6"
_MAX_INPUT_BYTES = 8 * 1024 * 1024
_DIGEST_LENGTH = 64
_REQUIRED_RISK_CLASSES = frozenset({
    "factuality", "entailment", "relevance", "direct_conflict",
    "temporal_conflict", "scope_conflict", "implicit_conflict", "injection",
    "cross_project", "retrieval_pressure", "abstention",
})
_SEALED_CRITICAL_RISK_CLASSES = frozenset({
    "direct_conflict", "temporal_conflict", "scope_conflict", "implicit_conflict",
    "injection", "cross_project", "retrieval_pressure", "abstention",
})
_V6_SEALED_CONFLICT_CLASSES = frozenset({
    "direct_conflict", "temporal_conflict", "scope_conflict", "implicit_conflict",
})
_MINIMUM_SEALED_CASE_COUNT = 400
_MINIMUM_SEALED_SUPPORT_ROW_COUNT = 800
_REQUIRED_SEALED_REPEATS = 3
_MINIMUM_WILSON_LOWER_95 = 0.95
_MINIMUM_STRATUM_SUPPORT = 80
_REQUIRED_STRATIFIED_LABELS: dict[str, tuple[str, ...]] = {
    "factuality": ("false", "ambiguous", "insufficient"),
    "entailment": ("contradicted", "insufficient"),
    "support_relevance": ("irrelevant",),
    "conflict": ("direct", "temporal", "scope", "implicit"),
}


class SemanticAssuranceReadinessError(ValueError):
    """A readiness evidence package is incomplete, weak, or unbound."""





def _risk_list(value: object, expected: frozenset[str], field: str) -> list[str]:
    if not isinstance(value, list) or set(value) != expected or len(value) != len(expected) or any(not isinstance(item, str) for item in value):
        raise SemanticAssuranceReadinessError(f"{field} is invalid")
    return sorted(expected)


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or value < 1:
        raise SemanticAssuranceReadinessError(f"{field} is invalid")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise SemanticAssuranceReadinessError(f"{field} is invalid")
    return value


def _rate(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
        raise SemanticAssuranceReadinessError(f"{field} is invalid")
    return round(float(value), 6)


def _digest_text(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != _DIGEST_LENGTH or any(character not in "0123456789abcdef" for character in value):
        raise SemanticAssuranceReadinessError(f"{field} is invalid")
    return value


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise SemanticAssuranceReadinessError(f"{field} is invalid")
    return value.strip()


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SemanticAssuranceReadinessError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise SemanticAssuranceReadinessError(f"{field} is invalid")
    return text


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _architecture_invariants(value: object) -> dict[str, bool]:
    expected = {"sqlite_authoritative", "mem0_semantic_logical_layer", "qdrant_rebuildable_projection", "langgraph_orchestration", "direct_mem0_qdrant_writes_prohibited", "automatic_semantic_apply_no_go"}
    if not isinstance(value, Mapping) or set(value) != expected or any(value.get(key) is not True for key in expected):
        raise SemanticAssuranceReadinessError("readiness architecture invariants are invalid")
    return {key: True for key in sorted(expected)}


def _owner_charter(value: object, admission: Mapping[str, Any], live: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, str]:
    expected = {"schema_version", "charter_id_digest", "approved_at", "admission_digest", "evaluated_run_digest", "model_run_identity_digest", "threshold_policy_digest", "readiness_aggregation_authorized", "automatic_apply_authorized", "charter_digest"}
    if not isinstance(value, Mapping) or set(value) != expected or value.get("schema_version") != "bhm.semantic-assurance-owner-charter-binding.v1":
        raise SemanticAssuranceReadinessError("readiness owner charter is invalid")
    core = {key: value[key] for key in expected - {"charter_digest"}}
    digest = _digest_text(value.get("charter_digest"), "owner charter digest")
    if _digest(core) != digest:
        raise SemanticAssuranceReadinessError("readiness owner charter digest mismatch")
    if value.get("admission_digest") != admission["admission_digest"] or value.get("evaluated_run_digest") != live["evaluated_run_digest"] or value.get("model_run_identity_digest") != live["model_run_identity_digest"] or value.get("threshold_policy_digest") != policy["threshold_policy_digest"] or value.get("readiness_aggregation_authorized") is not True or value.get("automatic_apply_authorized") is not False:
        raise SemanticAssuranceReadinessError("readiness owner charter does not preserve no-go binding")
    _digest_text(value.get("charter_id_digest"), "owner charter id")
    _timestamp(value.get("approved_at"), "owner charter timestamp")
    return {"charter_digest": digest}


def _risk_coverage(value: object, admission: Mapping[str, Any], live: Mapping[str, Any]) -> dict[str, str]:
    expected = {"schema_version", "admission_digest", "evaluated_run_digest", "all_risk_classes", "sealed_holdout_risk_classes", "receipt_digest"}
    if not isinstance(value, Mapping) or set(value) != expected or value.get("schema_version") != "bhm.semantic-assurance-risk-coverage-receipt.v1":
        raise SemanticAssuranceReadinessError("readiness risk coverage receipt is invalid")
    core = {key: value[key] for key in expected - {"receipt_digest"}}
    digest = _digest_text(value.get("receipt_digest"), "risk coverage digest")
    if _digest(core) != digest or value.get("admission_digest") != admission["admission_digest"] or value.get("evaluated_run_digest") != live["evaluated_run_digest"]:
        raise SemanticAssuranceReadinessError("readiness risk coverage binding is invalid")
    _risk_list(value.get("all_risk_classes"), _REQUIRED_RISK_CLASSES, "risk coverage taxonomy")
    _risk_list(value.get("sealed_holdout_risk_classes"), _SEALED_CRITICAL_RISK_CLASSES, "risk coverage sealed taxonomy")
    return {"receipt_digest": digest}


def _shadow_human_oracle(value: object, admission: Mapping[str, Any], live: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, str]:
    expected = {"schema_version", "campaign_id_digest", "admission_digest", "evaluated_run_digest", "case_count", "human_oracle_case_count", "disagreement_count", "unresolved_disagreement_count", "execution", "receipt_digest"}
    if not isinstance(value, Mapping) or set(value) != expected or value.get("schema_version") != "bhm.semantic-assurance-shadow-human-oracle-receipt.v1":
        raise SemanticAssuranceReadinessError("readiness shadow evidence is invalid")
    core = {key: value[key] for key in expected - {"receipt_digest"}}
    digest = _digest_text(value.get("receipt_digest"), "shadow evidence digest")
    if _digest(core) != digest or value.get("admission_digest") != admission["admission_digest"] or value.get("evaluated_run_digest") != live["evaluated_run_digest"]:
        raise SemanticAssuranceReadinessError("readiness shadow evidence binding is invalid")
    counts = ("case_count", "human_oracle_case_count", "disagreement_count", "unresolved_disagreement_count")
    if any(not isinstance(value.get(key), int) or value[key] < 0 for key in counts) or value["case_count"] < policy["minimum_case_count"] or value["human_oracle_case_count"] != value["case_count"] or value["disagreement_count"] != 0 or value["unresolved_disagreement_count"] != 0:
        raise SemanticAssuranceReadinessError("readiness shadow evidence is weak or unresolved")
    expected_execution = {"read_only": True, "model_called": False, "sqlite_mutation": False, "mem0_mutation": False, "qdrant_mutation": False, "automatic_apply": False}
    if value.get("execution") != expected_execution:
        raise SemanticAssuranceReadinessError("readiness shadow evidence is not read-only")
    _digest_text(value.get("campaign_id_digest"), "shadow campaign id")
    return {"receipt_digest": digest}


def _sqlite_rehearsal(value: object, admission: Mapping[str, Any], live: Mapping[str, Any]) -> dict[str, str]:
    expected = {"schema_version", "rehearsal_id_digest", "admission_digest", "evaluated_run_digest", "execution", "receipt_digest"}
    if not isinstance(value, Mapping) or set(value) != expected or value.get("schema_version") != "bhm.semantic-assurance-disposable-sqlite-rehearsal-receipt.v1":
        raise SemanticAssuranceReadinessError("readiness SQLite rehearsal evidence is invalid")
    core = {key: value[key] for key in expected - {"receipt_digest"}}
    digest = _digest_text(value.get("receipt_digest"), "SQLite rehearsal digest")
    if _digest(core) != digest or value.get("admission_digest") != admission["admission_digest"] or value.get("evaluated_run_digest") != live["evaluated_run_digest"]:
        raise SemanticAssuranceReadinessError("readiness SQLite rehearsal binding is invalid")
    expected_execution = {"disposable_sqlite_only": True, "fault_injected": True, "rollback_verified": True, "outbox_rollback_verified": True, "production_sqlite_touched": False, "mem0_mutation": False, "qdrant_mutation": False, "automatic_apply": False}
    if value.get("execution") != expected_execution:
        raise SemanticAssuranceReadinessError("readiness SQLite rehearsal is incomplete or not disposable")
    _digest_text(value.get("rehearsal_id_digest"), "SQLite rehearsal id")
    return {"receipt_digest": digest}


def _metrics(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"factuality", "entailment", "conflict", "operation", "support_relevance", "conflict_detection"}:
        raise SemanticAssuranceReadinessError("readiness metrics schema is invalid")
    result: dict[str, Any] = {}
    for key in ("factuality", "entailment", "support_relevance"):
        metric = value.get(key)
        if not isinstance(metric, Mapping) or not isinstance(metric.get("total"), int) or metric["total"] < 1:
            raise SemanticAssuranceReadinessError("readiness metric sample is invalid")
        interval = metric.get("accuracy_wilson_95")
        if not isinstance(interval, Mapping) or set(interval) != {"rate", "lower_95", "upper_95"}:
            raise SemanticAssuranceReadinessError("readiness metric Wilson interval is invalid")
        result[key] = {"accuracy": _rate(metric.get("accuracy"), f"{key} accuracy"), "accuracy_wilson_lower_95": _rate(interval.get("lower_95"), f"{key} lower confidence bound"), "sample_count": metric["total"]}
    conflict = value.get("conflict_detection")
    if not isinstance(conflict, Mapping) or set(conflict) != {"true_positive", "false_negative", "false_positive", "recall", "recall_wilson_95"} or not isinstance(conflict.get("recall_wilson_95"), Mapping):
        raise SemanticAssuranceReadinessError("readiness conflict metric is invalid")
    result["conflict_detection"] = {"recall": _rate(conflict.get("recall"), "conflict recall"), "recall_wilson_lower_95": _rate(conflict["recall_wilson_95"].get("lower_95"), "conflict lower confidence bound"), "true_positive": _nonnegative_int(conflict.get("true_positive"), "conflict true positives"), "false_negative": _nonnegative_int(conflict.get("false_negative"), "conflict false negatives")}
    return result


def _stratified_policy_map(
    value: object,
    field: str,
    parser: Any,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != set(_REQUIRED_STRATIFIED_LABELS):
        raise SemanticAssuranceReadinessError("readiness threshold policy stratified minima are invalid")
    result: dict[str, dict[str, Any]] = {}
    for dimension, labels in _REQUIRED_STRATIFIED_LABELS.items():
        item = value.get(dimension)
        if not isinstance(item, Mapping) or set(item) != set(labels):
            raise SemanticAssuranceReadinessError("readiness threshold policy stratified minima are invalid")
        try:
            result[dimension] = {label: parser(item[label], f"{field} {dimension}.{label}") for label in labels}
        except SemanticAssuranceReadinessError as exc:
            raise SemanticAssuranceReadinessError("readiness threshold policy stratified minima are invalid") from exc
    return result


def _policy(value: object, expected_digest: str) -> dict[str, Any]:
    expected = {
        "schema_version", "policy_id", "minimum_case_count", "minimum_support_row_count",
        "minimum_repeats", "minimum_accuracy_lower_95", "minimum_conflict_recall_lower_95",
        "minimum_stratum_support", "minimum_stratum_exact_label_recall_lower_95",
        "unsafe_non_no_op_budget", "missed_conflict_budget", "required_risk_classes",
    }
    if not isinstance(value, Mapping) or set(value) != expected or value.get("schema_version") != "bhm.semantic-assurance-threshold-policy.v2":
        raise SemanticAssuranceReadinessError("readiness threshold policy is invalid")
    minimums = value.get("minimum_accuracy_lower_95")
    if not isinstance(minimums, Mapping) or set(minimums) != {"factuality", "entailment", "support_relevance"}:
        raise SemanticAssuranceReadinessError("readiness threshold policy accuracy minima are invalid")
    minimum_stratum_support = _stratified_policy_map(
        value.get("minimum_stratum_support"), "threshold policy stratum support",
        _positive_int,
    )
    minimum_stratum_lower = _stratified_policy_map(
        value.get("minimum_stratum_exact_label_recall_lower_95"),
        "threshold policy stratum lower bound", _rate,
    )
    normalized = {
        "schema_version": "bhm.semantic-assurance-threshold-policy.v2",
        "policy_id": _text(value.get("policy_id"), "threshold policy id", 160),
        "minimum_case_count": _positive_int(value.get("minimum_case_count"), "threshold policy case minimum"),
        "minimum_support_row_count": _positive_int(value.get("minimum_support_row_count"), "threshold policy support minimum"),
        "minimum_repeats": _positive_int(value.get("minimum_repeats"), "threshold policy repeat minimum"),
        "minimum_accuracy_lower_95": {key: _rate(minimums.get(key), f"threshold policy {key} lower bound") for key in sorted(minimums)},
        "minimum_conflict_recall_lower_95": _rate(value.get("minimum_conflict_recall_lower_95"), "threshold policy conflict lower bound"),
        "minimum_stratum_support": minimum_stratum_support,
        "minimum_stratum_exact_label_recall_lower_95": minimum_stratum_lower,
        "unsafe_non_no_op_budget": value.get("unsafe_non_no_op_budget"),
        "missed_conflict_budget": value.get("missed_conflict_budget"),
        "required_risk_classes": _risk_list(value.get("required_risk_classes"), _REQUIRED_RISK_CLASSES, "threshold policy risk classes"),
    }
    if (
        any(
            normalized["minimum_stratum_support"][dimension][label] < _MINIMUM_STRATUM_SUPPORT
            for dimension, labels in _REQUIRED_STRATIFIED_LABELS.items()
            for label in labels
        )
        or any(
            normalized["minimum_stratum_exact_label_recall_lower_95"][dimension][label]
            < _MINIMUM_WILSON_LOWER_95
            for dimension, labels in _REQUIRED_STRATIFIED_LABELS.items()
            for label in labels
        )
    ):
        raise SemanticAssuranceReadinessError("readiness threshold policy weakens stratified sealed minima")
    if (
        normalized["minimum_case_count"] < _MINIMUM_SEALED_CASE_COUNT
        or normalized["minimum_support_row_count"] < _MINIMUM_SEALED_SUPPORT_ROW_COUNT
        or normalized["minimum_repeats"] != _REQUIRED_SEALED_REPEATS
        or any(
            normalized["minimum_accuracy_lower_95"][key] < _MINIMUM_WILSON_LOWER_95
            for key in ("factuality", "entailment", "support_relevance")
        )
        or normalized["minimum_conflict_recall_lower_95"] < _MINIMUM_WILSON_LOWER_95
        or normalized["unsafe_non_no_op_budget"] != 0
        or normalized["missed_conflict_budget"] != 0
    ):
        raise SemanticAssuranceReadinessError("readiness threshold policy weakens sealed safety-case minima")
    # The pre-registration commits the exact policy declaration, including its
    # canonical JSON field/list representation.  Validation above separately
    # proves semantic completeness; this binding must not silently rewrite it.
    digest = _digest(dict(value))
    if digest != expected_digest:
        raise SemanticAssuranceReadinessError("readiness threshold policy does not bind the admitted pre-registration")
    return {**normalized, "threshold_policy_digest": digest}


def _stratified_metrics(results: object) -> dict[str, dict[str, dict[str, Any]]]:
    """Derive required label strata from recomputed canonical result rows only."""

    if not isinstance(results, list) or not results:
        raise SemanticAssuranceReadinessError("readiness stratified metrics are invalid")
    counts = {
        dimension: {label: {"expected_count": 0, "correct_count": 0} for label in labels}
        for dimension, labels in _REQUIRED_STRATIFIED_LABELS.items()
    }
    for row in results:
        if not isinstance(row, Mapping):
            raise SemanticAssuranceReadinessError("readiness stratified metrics are invalid")
        expected = row.get("expected")
        prediction = row.get("prediction")
        if not isinstance(expected, Mapping) or not isinstance(prediction, Mapping):
            raise SemanticAssuranceReadinessError("readiness stratified metrics are invalid")
        for dimension in ("factuality", "entailment", "conflict"):
            label = expected.get(dimension)
            if label in counts[dimension]:
                counts[dimension][label]["expected_count"] += 1
                if prediction.get(dimension) == label:
                    counts[dimension][label]["correct_count"] += 1
        expected_relevance = expected.get("support_relevance")
        predicted_relevance = prediction.get("support_relevance")
        if not isinstance(expected_relevance, Mapping) or not isinstance(predicted_relevance, Mapping):
            raise SemanticAssuranceReadinessError("readiness stratified metrics are invalid")
        if set(expected_relevance) != set(predicted_relevance):
            raise SemanticAssuranceReadinessError("readiness stratified metrics are invalid")
        for support_id, label in expected_relevance.items():
            if label == "irrelevant":
                counts["support_relevance"]["irrelevant"]["expected_count"] += 1
                if predicted_relevance.get(support_id) == label:
                    counts["support_relevance"]["irrelevant"]["correct_count"] += 1
    return {
        dimension: {
            label: {
                "expected_count": item["expected_count"],
                "correct_count": item["correct_count"],
                "exact_label_recall": round(item["correct_count"] / item["expected_count"], 6)
                if item["expected_count"] else 0.0,
                "exact_label_recall_wilson_95": wilson_interval(
                    item["correct_count"], item["expected_count"],
                ) if item["expected_count"] else {"rate": 0.0, "lower_95": 0.0, "upper_95": 0.0},
            }
            for label, item in labels.items()
        }
        for dimension, labels in counts.items()
    }


def _risk_stratified_metrics(results: object) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    """Derive V6 risk/label recall and rebind its declared sealed matrix."""

    if not isinstance(results, list) or not results:
        raise SemanticAssuranceReadinessError("readiness risk-stratified metrics are invalid")
    counts = {
        risk: {
            dimension: {label: {"expected_count": 0, "correct_count": 0} for label in labels}
            for dimension, labels in _REQUIRED_STRATIFIED_LABELS.items()
        }
        for risk in _REQUIRED_RISK_CLASSES
    }
    case_rows: list[dict[str, Any]] = []
    adjudications: dict[str, dict[str, Any]] = {}
    for row in results:
        if not isinstance(row, Mapping):
            raise SemanticAssuranceReadinessError("readiness risk-stratified metrics are invalid")
        risk = row.get("risk_class")
        case_id = row.get("case_id")
        expected = row.get("expected")
        prediction = row.get("prediction")
        if risk not in _REQUIRED_RISK_CLASSES or not isinstance(case_id, str) or not isinstance(expected, Mapping) or not isinstance(prediction, Mapping):
            raise SemanticAssuranceReadinessError("readiness risk-stratified metrics are invalid")
        case_rows.append({"case_id": case_id, "partition": "sealed_holdout", "risk_class": risk})
        adjudications[case_id] = {"labels": expected}
        for dimension in ("factuality", "entailment", "conflict"):
            label = expected.get(dimension)
            if label in counts[risk][dimension]:
                counts[risk][dimension][label]["expected_count"] += 1
                if prediction.get(dimension) == label:
                    counts[risk][dimension][label]["correct_count"] += 1
        expected_relevance = expected.get("support_relevance")
        predicted_relevance = prediction.get("support_relevance")
        if not isinstance(expected_relevance, Mapping) or not isinstance(predicted_relevance, Mapping) or set(expected_relevance) != set(predicted_relevance):
            raise SemanticAssuranceReadinessError("readiness risk-stratified relevance is invalid")
        for support_id, label in expected_relevance.items():
            if label == "irrelevant":
                counts[risk]["support_relevance"]["irrelevant"]["expected_count"] += 1
                if predicted_relevance.get(support_id) == label:
                    counts[risk]["support_relevance"]["irrelevant"]["correct_count"] += 1
    try:
        from .semantic_assurance_label_admission import (
            _v6_sealed_risk_label_matrix_binding,
        )

        matrix = _v6_sealed_risk_label_matrix_binding(case_rows, adjudications)
    except Exception as exc:  # broad: convert risk-label matrix failures into fail-closed readiness errors
        raise SemanticAssuranceReadinessError("readiness risk-label matrix is invalid") from exc
    return {
        risk: {
            dimension: {
                label: {
                    "expected_count": item["expected_count"],
                    "correct_count": item["correct_count"],
                    "exact_label_recall": round(item["correct_count"] / item["expected_count"], 6) if item["expected_count"] else 0.0,
                    "exact_label_recall_wilson_95": wilson_interval(item["correct_count"], item["expected_count"])
                    if item["expected_count"] else {"rate": 0.0, "lower_95": 0.0, "upper_95": 0.0},
                }
                for label, item in labels.items()
            }
            for dimension, labels in dimensions.items()
        }
        for risk, dimensions in counts.items()
    } | {"_matrix_binding": matrix}


def _metric_gate(
    metrics: Mapping[str, Any],
    stratified_metrics: Mapping[str, Any],
    counts: Mapping[str, Any],
    repeat: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> bool:
    if (
        counts["case_count"] < policy["minimum_case_count"]
        or counts["support_row_count"] < policy["minimum_support_row_count"]
        or repeat["requested"] != _REQUIRED_SEALED_REPEATS
        or repeat["completed"] != _REQUIRED_SEALED_REPEATS
    ):
        raise SemanticAssuranceReadinessError("readiness sample minimum is not met")
    for key in ("factuality", "entailment", "support_relevance"):
        if metrics[key]["accuracy_wilson_lower_95"] < policy["minimum_accuracy_lower_95"][key]:
            raise SemanticAssuranceReadinessError("readiness lower confidence bound is below policy")
    if metrics["conflict_detection"]["recall_wilson_lower_95"] < policy["minimum_conflict_recall_lower_95"] or metrics["conflict_detection"]["false_negative"] != 0:
        raise SemanticAssuranceReadinessError("readiness conflict-detection confidence is below policy")
    for dimension, labels in _REQUIRED_STRATIFIED_LABELS.items():
        metrics_for_dimension = stratified_metrics.get(dimension)
        if not isinstance(metrics_for_dimension, Mapping) or set(metrics_for_dimension) != set(labels):
            raise SemanticAssuranceReadinessError("readiness stratified metrics are invalid")
        for label in labels:
            metric = metrics_for_dimension.get(label)
            if not isinstance(metric, Mapping) or set(metric) != {
                "expected_count", "correct_count", "exact_label_recall", "exact_label_recall_wilson_95",
            }:
                raise SemanticAssuranceReadinessError("readiness stratified metrics are invalid")
            expected_count = metric.get("expected_count")
            correct_count = metric.get("correct_count")
            interval = metric.get("exact_label_recall_wilson_95")
            if (
                not isinstance(expected_count, int)
                or expected_count < 0
                or not isinstance(correct_count, int)
                or not 0 <= correct_count <= expected_count
                or not isinstance(interval, Mapping)
                or set(interval) != {"rate", "lower_95", "upper_95"}
            ):
                raise SemanticAssuranceReadinessError("readiness stratified metrics are invalid")
            expected_interval = wilson_interval(correct_count, expected_count) if expected_count else None
            if (
                _rate(metric.get("exact_label_recall"), "stratified exact label recall")
                != (round(correct_count / expected_count, 6) if expected_count else 0.0)
                or expected_interval is None
                or dict(interval) != expected_interval
            ):
                raise SemanticAssuranceReadinessError("readiness stratified metrics are invalid")
            if expected_count < policy["minimum_stratum_support"][dimension][label]:
                raise SemanticAssuranceReadinessError("readiness stratified sample minimum is not met")
            if interval["lower_95"] < policy["minimum_stratum_exact_label_recall_lower_95"][dimension][label]:
                raise SemanticAssuranceReadinessError("readiness stratified lower confidence bound is below policy")
    return True


def _offline_report(
    value: object,
    admission: Mapping[str, Any],
    dataset: Mapping[str, Any],
    model: Mapping[str, str],
    prompt_digest: str,
    *,
    v6_label_admission: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema_version") != "bhm.semantic-assurance-receipt.v1":
        raise SemanticAssuranceReadinessError("readiness offline report is invalid")
    expected = {"schema_version", "mode", "dataset", "prediction_digest", "results_digest", "invocation", "counts", "metrics", "safety", "quality_gate", "gate", "results", "execution", "limitations", "report_digest"}
    if set(value) != expected:
        raise SemanticAssuranceReadinessError("readiness offline report contains unexpected fields")
    core = {key: value[key] for key in expected - {"report_digest"}}
    report_digest = _digest_text(value.get("report_digest"), "offline report digest")
    if _digest(core) != report_digest:
        raise SemanticAssuranceReadinessError("readiness offline report digest mismatch")
    report_dataset = value.get("dataset")
    expected_dataset = {
        **dict(dataset),
        "partition": "sealed_holdout",
        "label_independence": "attested_structurally_bound_with_corpus_pre_admission_not_identity_authenticated",
        "label_admission_version": "v2_corpus_pre_admitted",
        "human_label_admission_digest": admission["admission_digest"],
        "corpus_pre_admission_digest": admission["corpus"]["corpus_pre_admission_digest"],
    }
    if v6_label_admission is not None:
        if v6_label_admission.get("receipt_schema_version") in {
            LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V5,
            LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V6,
        }:
            expected_dataset.update({
                "label_independence": "attested_structurally_bound_with_v6_curator_audited_corpus_not_identity_authenticated",
                "label_admission_version": (
                    "v6_v6_curator_audited_risk_matrix_bound"
                    if v6_label_admission.get("receipt_schema_version") == LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V6
                    else "v5_v6_curator_audited_strata_bound"
                ),
                "admission_profile": v6_label_admission["admission_profile"],
                "partition_risk_counts": v6_label_admission["partition_risk_counts"],
                "v6_control_binding_digest": v6_label_admission["v6_control_binding_digest"],
                "curator_audit": v6_label_admission["curator_audit"],
                "sealed_readiness_strata_binding_digest": v6_label_admission["sealed_readiness_strata_binding_digest"],
                "sealed_readiness_strata_minimum_population": 80,
            })
            if v6_label_admission.get("receipt_schema_version") == LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V6:
                expected_dataset.update({
                    "sealed_risk_label_matrix_binding_digest": v6_label_admission["sealed_risk_label_matrix_binding_digest"],
                    "sealed_risk_label_matrix_minimum_risk_classes": 2,
                    "sealed_aligned_conflict_label_minimum_population": 50,
                })
        else:
            expected_dataset.update({
                "label_independence": "attested_structurally_bound_with_v6_corpus_pre_admission_not_identity_authenticated",
                "label_admission_version": "v3_v6_corpus_pre_admitted",
                "admission_profile": v6_label_admission["admission_profile"],
                "partition_risk_counts": v6_label_admission["partition_risk_counts"],
                "v6_control_binding_digest": v6_label_admission["v6_control_binding_digest"],
            })
    if report_dataset != expected_dataset:
        raise SemanticAssuranceReadinessError("readiness offline report corpus or admission binding is invalid")
    invocation = value.get("invocation")
    if not isinstance(invocation, Mapping) or set(invocation) - {"model_id", "model_variant", "prompt_digest", "endpoint", "repeat_number", "case_count", "case_id_set_digest"} or invocation.get("model_id") != model["model_id"] or invocation.get("model_variant") != model["model_variant"] or invocation.get("endpoint") != model["endpoint"] or invocation.get("prompt_digest") != prompt_digest:
        raise SemanticAssuranceReadinessError("readiness offline report model/run identity mismatch")
    if model != {"model_id": DEFAULT_MODEL_ID, "model_variant": DEFAULT_MODEL_VARIANT, "endpoint": DEFAULT_BASE_URL}:
        raise SemanticAssuranceReadinessError("readiness offline report does not bind the frozen local model contract")
    if prompt_digest != _digest(PROMPT_CONTRACT):
        raise SemanticAssuranceReadinessError("readiness offline report prompt contract is invalid")
    counts = value.get("counts")
    if not isinstance(counts, Mapping) or set(counts) != {"case_count", "support_row_count", "exact_case_match_count", "exact_case_mismatch_count"} or any(not isinstance(counts.get(key), int) or counts[key] < 0 for key in counts) or counts["case_count"] < 1 or counts["exact_case_match_count"] + counts["exact_case_mismatch_count"] != counts["case_count"]:
        raise SemanticAssuranceReadinessError("readiness offline report counts are invalid")
    metrics = _metrics(value.get("metrics"))
    safety = value.get("safety")
    expected_safety_keys = {"unsafe_non_no_op_count", "missed_conflict_count", "required_abstention_violation_count", "unnecessary_abstention_count"}
    if not isinstance(safety, Mapping) or set(safety) != expected_safety_keys or any(not isinstance(safety.get(key), int) or safety[key] < 0 for key in safety) or safety["unsafe_non_no_op_count"] != 0 or safety["missed_conflict_count"] != 0:
        raise SemanticAssuranceReadinessError("readiness offline report has unsafe or missed-conflict outcomes")
    try:
        derived = recompute_semantic_assurance_results(value.get("results"))
    except Exception as exc:  # broad: convert derived-result failures into fail-closed readiness errors
        raise SemanticAssuranceReadinessError("readiness offline results cannot be recomputed") from exc
    if value.get("prediction_digest") != derived["prediction_digest"] or value.get("results_digest") != derived["results_digest"]:
        raise SemanticAssuranceReadinessError("readiness offline prediction or result digest is not derived from results")
    if dict(counts) != {
        "case_count": derived["case_count"],
        "support_row_count": derived["support_row_count"],
        "exact_case_match_count": sum(item["exact_match"] for item in derived["results"]),
        "exact_case_mismatch_count": sum(not item["exact_match"] for item in derived["results"]),
    } or value.get("metrics") != derived["metrics"] or dict(safety) != derived["safety"]:
        raise SemanticAssuranceReadinessError("readiness offline aggregates do not match canonical results")
    expected_case_binding = admission["corpus_admission"].get("sealed_case_binding_digest")
    if expected_case_binding and derived["case_id_set_digest"] != expected_case_binding:
        raise SemanticAssuranceReadinessError("readiness offline result case coverage does not bind sealed corpus")
    gate = value.get("gate")
    if not isinstance(gate, Mapping) or gate.get("mechanical_valid") is not True or gate.get("safety_case_eligible") is not True or gate.get("automatic_apply_ready") is not False or gate.get("automatic_apply_decision") != "no_go":
        raise SemanticAssuranceReadinessError("readiness offline report gate is invalid")
    quality_gate = value.get("quality_gate")
    if not isinstance(quality_gate, Mapping) or quality_gate.get("unsafe_non_no_op_budget") != 0 or quality_gate.get("conflict_recall_required") != 1.0 or quality_gate.get("automatic_apply_decision") != "no_go":
        raise SemanticAssuranceReadinessError("readiness offline report quality boundary is invalid")
    result = {
        "report_digest": report_digest,
        "counts": dict(counts),
        "metrics": metrics,
        "stratified_metrics": _stratified_metrics(derived["results"]),
        "safety": dict(safety),
        "quality_gate": dict(quality_gate),
        "prediction_digest": str(value["prediction_digest"]),
        "results_digest": str(value["results_digest"]),
        "case_id_set_digest": derived["case_id_set_digest"],
        "case_ids": tuple(sorted(item["case_id"] for item in derived["results"])),
    }
    if (
        v6_label_admission is not None
        and v6_label_admission.get("receipt_schema_version") == LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V6
    ):
        risk_stratified_metrics = _risk_stratified_metrics(derived["results"])
        result["risk_label_matrix_binding"] = risk_stratified_metrics.pop("_matrix_binding")
        result["risk_stratified_metrics"] = risk_stratified_metrics
    return result
