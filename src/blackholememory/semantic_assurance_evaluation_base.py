"""Base data structures, schemas, errors, and validation for semantic-assurance evaluation."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "bhm.semantic-assurance.v1"
RECEIPT_SCHEMA_VERSION = "bhm.semantic-assurance-receipt.v1"
PREDICTION_SCHEMA_VERSION = "bhm.semantic-assurance-predictions.v1"

PARTITIONS = frozenset({"development", "validation", "sealed_holdout"})
FACTUALITY_LABELS = ("true", "false", "insufficient", "ambiguous")
ENTAILMENT_LABELS = ("supported", "contradicted", "insufficient")
CONFLICT_LABELS = ("none", "direct", "temporal", "scope", "implicit")
RELEVANCE_LABELS = ("relevant", "irrelevant")
OPERATION_LABELS = ("no_op", "create", "revise", "supersede", "archive", "link")
_ISOLATION_KEYS = ("family_id", "group_id", "source_group_id", "source_theme", "prompt_template", "prompt_template_id", "paraphrase_cluster_id")
_REQUIRED_EXPECTED_KEYS = frozenset({"factuality", "entailment", "conflict", "support_relevance", "operation"})


class SemanticAssuranceEvaluationError(ValueError):
    """The fixture, predictions, or strict safety contract is invalid."""



def _partition_name(value: Any) -> str:
    return _enum(value, tuple(sorted(PARTITIONS)), "semantic assurance partition")


def _enum(value: Any, allowed: Sequence[str], field: str) -> str:
    text = _text(value, field, 120).casefold()
    if text not in allowed:
        raise SemanticAssuranceEvaluationError(f"{field} is invalid")
    return text


def _text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise SemanticAssuranceEvaluationError(f"{field} is invalid")
    return text


def _sha256_digest(value: Any, field: str) -> str:
    text = _text(value, field, 64).casefold()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise SemanticAssuranceEvaluationError(f"{field} must be a SHA-256 digest")
    return text


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def wilson_interval(successes: int, total: int, *, z: float = 1.959963984540054) -> dict[str, float]:
    """Return a deterministic two-sided Wilson interval for a binomial rate."""

    if not isinstance(successes, int) or not isinstance(total, int) or total < 1 or not 0 <= successes <= total:
        raise SemanticAssuranceEvaluationError("wilson interval inputs are invalid")
    if not isinstance(z, (float, int)) or not math.isfinite(float(z)) or float(z) <= 0:
        raise SemanticAssuranceEvaluationError("wilson interval z score is invalid")
    rate = successes / total
    z_squared = float(z) ** 2
    denominator = 1.0 + z_squared / total
    centre = (rate + z_squared / (2.0 * total)) / denominator
    margin = float(z) * math.sqrt((rate * (1.0 - rate) + z_squared / (4.0 * total)) / total) / denominator
    return {"rate": round(rate, 6), "lower_95": round(max(0.0, centre - margin), 6), "upper_95": round(min(1.0, centre + margin), 6)}



def _validate_dataset(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping) and raw.get("_semantic_assurance_normalized") is True:
        return dict(raw)
    if not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION:
        raise SemanticAssuranceEvaluationError("semantic assurance dataset schema is unsupported")
    dataset_id = _text(raw.get("dataset_id"), "semantic assurance dataset id", 120)
    if not isinstance(raw.get("labeling"), Mapping) or not isinstance(raw.get("label_provenance", raw.get("labeling")), Mapping):
        raise SemanticAssuranceEvaluationError("semantic assurance labeling metadata is invalid")
    split_contract = raw.get("split_contract")
    if not isinstance(split_contract, Mapping) or split_contract.get("labels_excluded_from_model_input") is not True:
        raise SemanticAssuranceEvaluationError("semantic assurance split contract must exclude labels from model input")
    declared_partitions = split_contract.get("partitions")
    if not isinstance(declared_partitions, list) or set(declared_partitions) != PARTITIONS:
        raise SemanticAssuranceEvaluationError("semantic assurance partitions are invalid")
    splits = raw.get("splits")
    if not isinstance(splits, Mapping) or set(splits) != PARTITIONS:
        raise SemanticAssuranceEvaluationError("semantic assurance split metadata is invalid")
    boundary = raw.get("evaluation_boundary")
    if not isinstance(boundary, Mapping) or any(boundary.get(key) is not expected for key, expected in {
        "proposal_only": True,
        "sqlite_mutation": False,
        "mem0_mutation": False,
        "qdrant_mutation": False,
        "automatic_apply": False,
    }.items()):
        raise SemanticAssuranceEvaluationError("semantic assurance boundary contract is unsafe")
    quality_gate = _validate_quality_gate(raw.get("quality_gate"))
    groups = _validate_groups(raw.get("groups"))
    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SemanticAssuranceEvaluationError("semantic assurance dataset must contain cases")
    normalized_cases = [_validate_case(case, groups) for case in cases]
    if len({case["case_id"] for case in normalized_cases}) != len(normalized_cases):
        raise SemanticAssuranceEvaluationError("semantic assurance case ids must be unique")
    if {case["partition"] for case in normalized_cases} != PARTITIONS:
        raise SemanticAssuranceEvaluationError("semantic assurance dataset must cover every partition")
    _validate_split_isolation(normalized_cases)
    labels_only = [{
        "case_id": case["case_id"],
        "partition": case["partition"],
        "expected": case["expected"],
    } for case in normalized_cases]
    return {
        "_semantic_assurance_normalized": True,
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "labeling_kind": _text(raw["labeling"].get("kind"), "semantic assurance labeling kind", 120),
        "label_provenance_kind": _text(raw["label_provenance"].get("kind"), "semantic assurance label provenance kind", 120),
        "cases": normalized_cases,
        "quality_gate": quality_gate,
        "fixture_digest": _digest(raw),
        "label_digest": _digest(labels_only),
    }


def _validate_quality_gate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "unsafe_non_no_op_budget", "conflict_recall_required", "unsupported_or_ambiguous_operation", "automatic_apply_decision"
    }:
        raise SemanticAssuranceEvaluationError("semantic assurance quality gate is invalid")
    budget = raw.get("unsafe_non_no_op_budget")
    if not isinstance(budget, int) or budget != 0:
        raise SemanticAssuranceEvaluationError("semantic assurance quality gate must retain zero unsafe-operation budget")
    try:
        conflict_recall = float(raw.get("conflict_recall_required"))
    except (TypeError, ValueError) as exc:
        raise SemanticAssuranceEvaluationError("semantic assurance conflict recall gate is invalid") from exc
    if conflict_recall != 1.0 or raw.get("unsupported_or_ambiguous_operation") != "no_op" or raw.get("automatic_apply_decision") != "no_go":
        raise SemanticAssuranceEvaluationError("semantic assurance quality gate is unsafe")
    return {
        "unsafe_non_no_op_budget": 0,
        "conflict_recall_required": 1.0,
        "unsupported_or_ambiguous_operation": "no_op",
        "automatic_apply_decision": "no_go",
    }


def _validate_groups(raw: Any) -> dict[str, str]:
    if not isinstance(raw, list) or not raw:
        raise SemanticAssuranceEvaluationError("semantic assurance groups are invalid")
    result: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise SemanticAssuranceEvaluationError("semantic assurance group is invalid")
        group_id = _text(item.get("group_id"), "semantic assurance group id", 120)
        partition = _partition_name(item.get("partition"))
        if group_id in result:
            raise SemanticAssuranceEvaluationError("semantic assurance group ids must be unique")
        result[group_id] = partition
    return result


def _validate_case(raw: Any, groups: Mapping[str, str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SemanticAssuranceEvaluationError("semantic assurance case is invalid")
    case_id = _text(raw.get("case_id"), "semantic assurance case id", 120)
    family_id = _text(raw.get("family_id"), "semantic assurance family id", 160)
    group_id = _text(raw.get("group_id"), "semantic assurance group id", 120)
    partition = _partition_name(raw.get("partition"))
    if groups.get(group_id) != partition:
        raise SemanticAssuranceEvaluationError("semantic assurance case group partition is invalid")
    risk_class = _text(raw.get("risk_class"), "semantic assurance risk class", 120)
    supports = raw.get("supports")
    if not isinstance(supports, list) or not supports:
        raise SemanticAssuranceEvaluationError("semantic assurance supports are invalid")
    support_ids = []
    for support in supports:
        if not isinstance(support, Mapping):
            raise SemanticAssuranceEvaluationError("semantic assurance support is invalid")
        support_ids.append(_text(support.get("support_id"), "semantic assurance support id", 120))
    if len(set(support_ids)) != len(support_ids):
        raise SemanticAssuranceEvaluationError("semantic assurance support ids must be unique per case")
    expected = _validate_labels(raw.get("expected"), support_ids, field="expected")
    isolation = {"family_id": family_id, "group_id": group_id}
    for key in _ISOLATION_KEYS[2:]:
        if key in raw:
            isolation[key] = _text(raw.get(key), f"semantic assurance {key}", 160)
    # Preserve only opaque support identities.  They are required to bind
    # independent relevance labels, but reveal neither prompt nor evidence.
    return {
        "case_id": case_id,
        "partition": partition,
        "support_ids": tuple(sorted(support_ids)),
        "risk_class": risk_class,
        "expected": expected,
        "isolation": isolation,
    }


def _validate_prediction(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SemanticAssuranceEvaluationError("semantic assurance prediction is invalid")
    allowed = {"case_id", *_REQUIRED_EXPECTED_KEYS}
    if set(raw) != allowed:
        raise SemanticAssuranceEvaluationError("semantic assurance prediction must be content-free classification only")
    case_id = _text(raw.get("case_id"), "semantic assurance prediction case id", 120)
    labels = _validate_labels({key: raw[key] for key in _REQUIRED_EXPECTED_KEYS}, None, field="prediction")
    return {"case_id": case_id, **labels}


def _validate_labels(raw: Any, support_ids: Sequence[str] | None, *, field: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _REQUIRED_EXPECTED_KEYS:
        raise SemanticAssuranceEvaluationError(f"semantic assurance {field} labels are incomplete")
    factuality = _enum(raw.get("factuality"), FACTUALITY_LABELS, f"semantic assurance {field} factuality")
    entailment = _enum(raw.get("entailment"), ENTAILMENT_LABELS, f"semantic assurance {field} entailment")
    conflict = _enum(raw.get("conflict"), CONFLICT_LABELS, f"semantic assurance {field} conflict")
    operation = _enum(raw.get("operation"), OPERATION_LABELS, f"semantic assurance {field} operation")
    relevance = raw.get("support_relevance")
    if not isinstance(relevance, Mapping) or not relevance:
        raise SemanticAssuranceEvaluationError(f"semantic assurance {field} relevance is invalid")
    relevance_normalized = {
        _text(key, f"semantic assurance {field} support id", 120): _enum(value, RELEVANCE_LABELS, f"semantic assurance {field} relevance")
        for key, value in relevance.items()
    }
    if len(relevance_normalized) != len(relevance):
        raise SemanticAssuranceEvaluationError(f"semantic assurance {field} relevance ids are invalid")
    if support_ids is not None and set(relevance_normalized) != set(support_ids):
        raise SemanticAssuranceEvaluationError(f"semantic assurance {field} relevance must cover exact supports")
    return {
        "factuality": factuality,
        "entailment": entailment,
        "conflict": conflict,
        "operation": operation,
        "support_relevance": dict(sorted(relevance_normalized.items())),
    }


def _validate_split_isolation(cases: Sequence[Mapping[str, Any]]) -> None:
    observed: dict[tuple[str, str], str] = {}
    for case in cases:
        for key, value in dict(case["isolation"]).items():
            identity = (key, str(value))
            existing = observed.setdefault(identity, str(case["partition"]))
            if existing != case["partition"]:
                raise SemanticAssuranceEvaluationError(f"semantic assurance split leakage for {key}")



def load_semantic_assurance_dataset(path: Path | str) -> dict[str, Any]:
    """Load and validate a local semantic-assurance fixture.

    The normalized return value retains only classification metadata and labels,
    never fixture prompt/evidence text.  This makes it safe to pass onward to
    receipt generation without accidentally serialising source material.
    """

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticAssuranceEvaluationError("semantic assurance dataset is unreadable") from exc
    return _validate_dataset(raw)



def load_semantic_assurance_predictions(path: Path | str) -> list[dict[str, Any]]:
    """Load bounded, content-free local classifier output rows."""

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticAssuranceEvaluationError("semantic assurance predictions are unreadable") from exc
    if isinstance(raw, Mapping):
        schema = raw.get("schema_version")
        if schema not in {None, PREDICTION_SCHEMA_VERSION}:
            raise SemanticAssuranceEvaluationError("semantic assurance prediction schema is unsupported")
        raw = raw.get("predictions")
    if not isinstance(raw, list) or not raw:
        raise SemanticAssuranceEvaluationError("semantic assurance predictions must be a non-empty list")
    normalized = [_validate_prediction(item) for item in raw]
    if len({item["case_id"] for item in normalized}) != len(normalized):
        raise SemanticAssuranceEvaluationError("semantic assurance prediction case ids must be unique")
    return normalized



__all__ = [
    "CONFLICT_LABELS",
    "ENTAILMENT_LABELS",
    "FACTUALITY_LABELS",
    "OPERATION_LABELS",
    "PARTITIONS",
    "PREDICTION_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION",
    "RELEVANCE_LABELS",
    "SCHEMA_VERSION",
    "SemanticAssuranceEvaluationError",
    "_ISOLATION_KEYS",
    "_REQUIRED_EXPECTED_KEYS",
    "_digest",
    "_enum",
    "_partition_name",
    "_sha256_digest",
    "_text",
    "_validate_case",
    "_validate_dataset",
    "_validate_groups",
    "_validate_labels",
    "_validate_prediction",
    "_validate_quality_gate",
    "_validate_split_isolation",
    "load_semantic_assurance_dataset",
    "load_semantic_assurance_predictions",
    "wilson_interval",
]
