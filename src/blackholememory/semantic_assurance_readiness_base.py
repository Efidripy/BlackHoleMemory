"""Base schema versions, receipt persistence, and baseline readiness assessment."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .semantic_assurance_label_admission import (
    SemanticAssuranceLabelAdmissionError,
    verify_semantic_assurance_label_admission,
)
from .semantic_assurance_readiness_evidence import (
    _live_receipt,
)
from .semantic_assurance_readiness_metrics import (
    SemanticAssuranceReadinessError,
    _REQUIRED_RISK_CLASSES,
    _architecture_invariants,
    _digest,
    _digest_text,
    _metric_gate,
    _owner_charter,
    _policy,
    _risk_coverage,
    _shadow_human_oracle,
    _sqlite_rehearsal,
)


SCHEMA_VERSION = "bhm.semantic-assurance-readiness.v2"
RECEIPT_SCHEMA_VERSION = "bhm.semantic-assurance-readiness-receipt.v2"
SCHEMA_VERSION_V3 = "bhm.semantic-assurance-readiness.v3"
RECEIPT_SCHEMA_VERSION_V3 = "bhm.semantic-assurance-readiness-receipt.v3"
SCHEMA_VERSION_V4 = "bhm.semantic-assurance-readiness.v4"
RECEIPT_SCHEMA_VERSION_V4 = "bhm.semantic-assurance-readiness-receipt.v4"
SCHEMA_VERSION_V5 = "bhm.semantic-assurance-readiness.v5"
RECEIPT_SCHEMA_VERSION_V5 = "bhm.semantic-assurance-readiness-receipt.v5"
SCHEMA_VERSION_V6 = "bhm.semantic-assurance-readiness.v6"
RECEIPT_SCHEMA_VERSION_V6 = "bhm.semantic-assurance-readiness-receipt.v6"
SCHEMA_VERSION_V7 = "bhm.semantic-assurance-readiness.v7"
RECEIPT_SCHEMA_VERSION_V7 = "bhm.semantic-assurance-readiness-receipt.v7"
LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V2 = "bhm.semantic-assurance-human-label-admission-receipt.v2"


def assess_semantic_assurance_readiness(
    raw: object,
    *,
    _allow_live_v3: bool = False,
    _allow_live_v4: bool = False,
    _allow_live_v5: bool = False,
    _required_label_admission_schema: str = LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V2,
) -> dict[str, Any]:
    """Validate one immutable, content-free readiness evidence package.

    All nested receipts use exact schemas.  Unknown fields, a changed digest,
    weak lower confidence bound, or an absent independent gate reject the
    package rather than producing a partial readiness claim.
    """

    if not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION:
        raise SemanticAssuranceReadinessError("readiness evidence schema is unsupported")
    expected = {
        "schema_version", "admission_receipt", "live_receipt", "threshold_policy",
        "risk_coverage", "shadow_human_oracle", "sqlite_rehearsal",
        "architecture_invariants", "owner_charter",
    }
    if set(raw) != expected:
        raise SemanticAssuranceReadinessError("readiness evidence contains unexpected fields")

    admission = _admission(raw["admission_receipt"], required_receipt_schema=_required_label_admission_schema)
    policy = _policy(raw["threshold_policy"], admission["pre_registration"]["threshold_policy_digest"])
    live = _live_receipt(
        raw["live_receipt"],
        admission,
        policy,
        allow_live_v3=_allow_live_v3,
        allow_live_v4=_allow_live_v4,
        allow_live_v5=_allow_live_v5,
    )
    risk_coverage = _risk_coverage(raw["risk_coverage"], admission, live)
    shadow = _shadow_human_oracle(raw["shadow_human_oracle"], admission, live, policy)
    rehearsal = _sqlite_rehearsal(raw["sqlite_rehearsal"], admission, live)
    invariants = _architecture_invariants(raw["architecture_invariants"])
    charter = _owner_charter(raw["owner_charter"], admission, live, policy)

    metric_gate = _metric_gate(
        live["metrics"], live["stratified_metrics"], live["counts"], live["repeat"], policy,
    )
    checks = {
        "verified_human_label_admission": True,
        "exact_evaluated_run_identity": True,
        "declared_threshold_policy": True,
        "required_risk_coverage": True,
        "metric_thresholds_and_sample_minima": metric_gate,
        "derived_stratified_metric_thresholds_and_sample_minima": metric_gate,
        "zero_unsafe_and_missed_conflicts": True,
        "repeat_agreement": True,
        "read_only_shadow_human_oracle": True,
        "disposable_sqlite_rehearsal": True,
        "architecture_invariants": True,
        "separate_owner_charter_binding": True,
    }
    if not all(checks.values()):  # Defensive: helpers fail closed before here.
        raise SemanticAssuranceReadinessError("readiness evidence checks are incomplete")

    core = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "mode": "read-only-content-free-readiness-aggregation",
        "bindings": {
            "admission_digest": admission["admission_digest"],
            "evaluated_run_digest": live["evaluated_run_digest"],
            "model_run_identity_digest": live["model_run_identity_digest"],
            "dataset_binding_digest": live["dataset_binding_digest"],
            "prompt_contract_digest": live["prompt_contract_digest"],
            "threshold_policy_digest": policy["threshold_policy_digest"],
            "risk_coverage_digest": risk_coverage["receipt_digest"],
            "shadow_human_oracle_digest": shadow["receipt_digest"],
            "sqlite_rehearsal_digest": rehearsal["receipt_digest"],
            "owner_charter_digest": charter["charter_digest"],
        },
        "evidence": {
            "case_count": live["counts"]["case_count"],
            "support_row_count": live["counts"]["support_row_count"],
            "repeats": live["repeat"]["completed"],
            "metrics": live["metrics"],
            "stratified_metrics": live["stratified_metrics"],
            "unsafe_non_no_op_count": 0,
            "missed_conflict_count": 0,
            "shadow_disagreement_count": 0,
            "unresolved_shadow_disagreement_count": 0,
            "risk_class_count": len(_REQUIRED_RISK_CLASSES),
        },
        "architecture": invariants,
        "checks": checks,
        "status": "evidence_complete_but_release_not_authorized",
        "decision": {
            "automatic_apply_ready": False,
            "decision": "no_go",
            "reason": "readiness evidence aggregation is not deployment authorization",
        },
        "execution": {
            "read_only": True,
            "network_used": False,
            "model_called": False,
            "sqlite_mutation": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "automatic_apply": False,
        },
        "limitations": [
            "the receipt contains structural bindings and aggregates, not corpus, prompt, model-answer, path, or identity data",
            "human identities, semantic truth, and open-world safety are not proven by structural receipt validation",
            "automatic semantic apply remains no_go pending a separate owner release decision and out-of-band safety governance",
        ],
    }
    return {**core, "readiness_digest": _digest(core)}


def write_semantic_assurance_readiness_receipt(report: Mapping[str, Any], path: Path | str) -> None:
    """Write a content-free aggregate receipt to an explicit JSON path."""

    target = Path(path)
    if target.suffix.casefold() != ".json":
        raise SemanticAssuranceReadinessError("readiness receipt path must end in .json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _admission(
    value: object,
    *,
    required_receipt_schema: str = LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V2,
) -> dict[str, Any]:
    try:
        receipt = verify_semantic_assurance_label_admission(value)
    except SemanticAssuranceLabelAdmissionError as exc:
        raise SemanticAssuranceReadinessError("readiness human-label admission receipt is invalid") from exc
    if receipt.get("schema_version") != required_receipt_schema:
        if required_receipt_schema == LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V2:
            raise SemanticAssuranceReadinessError("readiness requires a v2 corpus-admitted human-label receipt")
        raise SemanticAssuranceReadinessError("readiness requires the exact declared human-label receipt version")
    corpus = receipt.get("corpus")
    corpus_admission = receipt.get("corpus_admission")
    if (
        not isinstance(corpus, Mapping)
        or not isinstance(corpus_admission, Mapping)
        or corpus.get("corpus_pre_admission_digest") != corpus_admission.get("admission_digest")
    ):
        raise SemanticAssuranceReadinessError("readiness v2 corpus pre-admission binding is invalid")
    _digest_text(corpus_admission.get("curator_attestation_digest"), "readiness v2 curator attestation digest")
    return receipt


__all__ = [
    "LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V2",
    "RECEIPT_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION_V3",
    "RECEIPT_SCHEMA_VERSION_V4",
    "RECEIPT_SCHEMA_VERSION_V5",
    "RECEIPT_SCHEMA_VERSION_V6",
    "RECEIPT_SCHEMA_VERSION_V7",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_V3",
    "SCHEMA_VERSION_V4",
    "SCHEMA_VERSION_V5",
    "SCHEMA_VERSION_V6",
    "SCHEMA_VERSION_V7",
    "SemanticAssuranceReadinessError",
    "_admission",
    "assess_semantic_assurance_readiness",
    "write_semantic_assurance_readiness_receipt",
]
