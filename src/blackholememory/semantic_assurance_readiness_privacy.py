"""Privacy-split V6 and V7 safety-case readiness assessment."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .semantic_assurance_live import PROMPT_CONTRACT
from .semantic_assurance_readiness_base import (
    RECEIPT_SCHEMA_VERSION_V6,
    RECEIPT_SCHEMA_VERSION_V7,
    SCHEMA_VERSION_V6,
    SCHEMA_VERSION_V7,
    _admission,
)
from .semantic_assurance_readiness_evidence import (
    _source_identity,
    _v6_curator_audited_label_admission_binding,
    _v6_private_public_live,
)
from .semantic_assurance_readiness_metrics import (
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V5,
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V6,
    SemanticAssuranceReadinessError,
    _REQUIRED_RISK_CLASSES,
    _architecture_invariants,
    _digest,
    _metric_gate,
    _owner_charter,
    _policy,
    _risk_coverage,
    _shadow_human_oracle,
    _sqlite_rehearsal,
)
from .semantic_assurance_runner_attestation import (
    SemanticAssuranceRunnerAttestationError,
    verify_semantic_assurance_runner_attestation,
)


def assess_semantic_assurance_readiness_v6(
    raw: object,
    *,
    trust_registry: Mapping[str, Any] | Path | str | None = None,
) -> dict[str, Any]:
    """Validate the historical privacy-split V6 safety-case package."""

    return _assess_privacy_split_readiness(
        raw,
        trust_registry=trust_registry,
        required_schema=SCHEMA_VERSION_V6,
        receipt_schema=RECEIPT_SCHEMA_VERSION_V6,
        require_risk_label_matrix=False,
    )


def assess_semantic_assurance_readiness_v7(
    raw: object,
    *,
    trust_registry: Mapping[str, Any] | Path | str | None = None,
) -> dict[str, Any]:
    """Validate the forward-only V7 package bound to the V6 risk-label matrix."""

    return _assess_privacy_split_readiness(
        raw,
        trust_registry=trust_registry,
        required_schema=SCHEMA_VERSION_V7,
        receipt_schema=RECEIPT_SCHEMA_VERSION_V7,
        require_risk_label_matrix=True,
    )


def _assess_privacy_split_readiness(
    raw: object,
    *,
    trust_registry: Mapping[str, Any] | Path | str | None,
    required_schema: str,
    receipt_schema: str,
    require_risk_label_matrix: bool,
) -> dict[str, Any]:
    """Validate a privacy-split V6 live-evaluation safety-case package.

    The public receipt contains no per-case semantic rows.  The corresponding
    owner-only bundle is mandatory input here: metrics are recomputed from its
    exact reports rather than accepted from public aggregates.  This function
    remains a reader and returns only a content-free readiness receipt.
    """

    tier = "v7" if require_risk_label_matrix else "v6"
    if not isinstance(raw, Mapping) or raw.get("schema_version") != required_schema:
        raise SemanticAssuranceReadinessError(f"readiness {tier} evidence schema is unsupported")
    expected = {
        "schema_version", "admission_receipt", "public_live_receipt",
        "private_evaluation_bundle", "threshold_policy", "risk_coverage",
        "shadow_human_oracle", "sqlite_rehearsal", "architecture_invariants",
        "owner_charter", "source_identity", "runner_attestation",
    }
    if set(raw) != expected:
        raise SemanticAssuranceReadinessError(f"readiness {tier} evidence contains unexpected fields")

    source = _source_identity(raw["source_identity"])
    admission = _admission(
        raw["admission_receipt"],
        required_receipt_schema=(
            LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V6
            if require_risk_label_matrix else LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V5
        ),
    )
    policy = _policy(raw["threshold_policy"], admission["pre_registration"]["threshold_policy_digest"])
    curator_binding = _v6_curator_audited_label_admission_binding(
        admission,
        require_risk_label_matrix=require_risk_label_matrix,
    )
    live = _v6_private_public_live(
        raw["public_live_receipt"],
        raw["private_evaluation_bundle"],
        admission,
        require_risk_label_matrix=require_risk_label_matrix,
    )
    risk_coverage = _risk_coverage(raw["risk_coverage"], admission, live)
    shadow = _shadow_human_oracle(raw["shadow_human_oracle"], admission, live, policy)
    rehearsal = _sqlite_rehearsal(raw["sqlite_rehearsal"], admission, live)
    invariants = _architecture_invariants(raw["architecture_invariants"])
    charter = _owner_charter(raw["owner_charter"], admission, live, policy)
    metric_gate = _metric_gate(
        live["metrics"], live["stratified_metrics"], live["counts"], live["repeat"], policy,
    )

    public = raw["public_live_receipt"]
    if not isinstance(public, Mapping):  # Defensive; the V6 verifier already checks it.
        raise SemanticAssuranceReadinessError(f"readiness {tier} public receipt is invalid")
    execution = public["execution"]
    assert isinstance(execution, Mapping)
    event_chain = execution["event_chain_commitment"]
    assert isinstance(event_chain, Mapping)
    pre_core = {
        "schema_version": (
            "bhm.semantic-assurance-pre-attestation-core.v5"
            if require_risk_label_matrix else "bhm.semantic-assurance-pre-attestation-core.v4"
        ),
        "source_identity": source,
        "v6_curator_audited_label_admission": curator_binding,
        "public_live_receipt_digest": live["public_receipt_digest"],
        "private_evaluation_bundle_digest": live["private_bundle_digest"],
        "evaluated_run_digest": live["evaluated_run_digest"],
        "event_chain_commitment": dict(event_chain),
        "metric_gate": metric_gate,
    }
    pre_digest = _digest(pre_core)
    expected_bindings = {
        "source_revision": source["source_revision"],
        "source_tree_sha256": source["source_tree_sha256"],
        "runner_manifest_digest": source["runner_manifest_digest"],
        "prompt_contract_digest": hashlib.sha256(PROMPT_CONTRACT.encode("utf-8")).hexdigest(),
        "corpus_admission_digest": curator_binding["corpus_admission_digest"],
        "label_admission_digest": curator_binding["admission_digest"],
        "threshold_policy_digest": policy["threshold_policy_digest"],
        "live_report_digest": live["legacy_live_receipt_digest"],
        "pre_attestation_readiness_digest": pre_digest,
        "event_count": event_chain["event_count"],
        "first_event_digest": event_chain["first_event_digest"],
        "last_event_digest": event_chain["last_event_digest"],
        "chain_root": event_chain["chain_root"],
        "inventory_before_digest": event_chain["inventory_before_digest"],
        "inventory_after_digest": event_chain["inventory_after_digest"],
    }
    try:
        runner_kwargs: dict[str, Any] = {"expected_bindings": expected_bindings}
        if trust_registry is not None:
            runner_kwargs["trust_registry"] = trust_registry
        runner = verify_semantic_assurance_runner_attestation(raw["runner_attestation"], **runner_kwargs)
    except (SemanticAssuranceRunnerAttestationError, TypeError, KeyError) as exc:
        raise SemanticAssuranceReadinessError(f"readiness {tier} trusted runner attestation is invalid") from exc

    checks = {
        "verified_human_label_admission": True,
        "v6_curator_audit_binding": True,
        "public_private_live_exact_binding": True,
        "private_reports_recomputed": True,
        "declared_threshold_policy": True,
        "required_risk_coverage": True,
        "metric_thresholds_and_sample_minima": metric_gate,
        "derived_stratified_metric_thresholds_and_sample_minima": metric_gate,
        "zero_unsafe_and_missed_conflicts": True,
        "repeat_agreement": True,
        "event_chain_commitment_bound": True,
        "read_only_shadow_human_oracle": True,
        "disposable_sqlite_rehearsal": True,
        "architecture_invariants": True,
        "separate_owner_charter_binding": True,
        "trusted_runner_attestation": True,
        "source_identity_binding": True,
        "pre_attestation_no_cycle": True,
    }
    if require_risk_label_matrix:
        checks["risk_label_matrix_recomputed"] = True
    if not all(checks.values()):
        raise SemanticAssuranceReadinessError(f"readiness {tier} evidence checks are incomplete")
    core = {
        "schema_version": receipt_schema,
        "mode": f"read-only-content-free-readiness-aggregation-{tier}",
        "bindings": {
            "pre_attestation_readiness_digest": pre_digest,
            "runner_attestation_digest": runner["attestation_digest"],
            "trust_registry_digest": runner["trusted_runner"]["trust_registry_digest"],
            "public_live_receipt_digest": live["public_receipt_digest"],
            "private_evaluation_bundle_digest": live["private_bundle_digest"],
            "legacy_live_receipt_digest": live["legacy_live_receipt_digest"],
            "offline_reports_commitment": live["offline_reports_commitment"],
            "evaluated_run_digest": live["evaluated_run_digest"],
            "admission_digest": curator_binding["admission_digest"],
            "corpus_pre_admission_digest": curator_binding["corpus_pre_admission_digest"],
            "corpus_admission_digest": curator_binding["corpus_admission_digest"],
            "admission_profile": curator_binding["admission_profile"],
            "partition_risk_counts": curator_binding["partition_risk_counts"],
            "v6_control_binding_digest": curator_binding["v6_control_binding_digest"],
            "curator_audit": curator_binding["curator_audit"],
            "sealed_readiness_strata_binding_digest": curator_binding["sealed_readiness_strata_binding_digest"],
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
            "reason": f"privacy-split {tier.upper()} safety-case evidence is not deployment authorization",
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
            "public V6 receipts intentionally exclude per-case semantic rows; recomputation occurs only from owner-only evidence",
            "a content-free event-chain commitment is signed but does not disclose the private event ledger",
            "human identity, semantic truth, and open-world safety are not proven by structural receipt validation",
            "automatic semantic apply remains no_go pending a separate owner release decision and out-of-band safety governance",
        ],
        "trusted_runner": runner["trusted_runner"],
    }
    if require_risk_label_matrix:
        core["bindings"].update({
            "sealed_risk_label_matrix_binding_digest": curator_binding["sealed_risk_label_matrix_binding_digest"],
            "sealed_risk_label_matrix_minimum_risk_classes": 2,
            "sealed_aligned_conflict_label_minimum_population": 50,
        })
        core["evidence"]["risk_stratified_metrics"] = live["risk_stratified_metrics"]
    return {**core, "readiness_digest": _digest(core)}


__all__ = [
    "_assess_privacy_split_readiness",
    "assess_semantic_assurance_readiness_v6",
    "assess_semantic_assurance_readiness_v7",
]
