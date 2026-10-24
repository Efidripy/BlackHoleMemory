"""Readiness assessment with trusted runner attestation across v3, v4, and v5."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .semantic_assurance_live import (
    LIVE_RECEIPT_SCHEMA_VERSION_V4,
    LIVE_RECEIPT_SCHEMA_VERSION_V5,
    PROMPT_CONTRACT,
)
from .semantic_assurance_readiness_base import (
    RECEIPT_SCHEMA_VERSION_V3,
    RECEIPT_SCHEMA_VERSION_V4,
    RECEIPT_SCHEMA_VERSION_V5,
    SCHEMA_VERSION,
    SCHEMA_VERSION_V3,
    SCHEMA_VERSION_V4,
    SCHEMA_VERSION_V5,
    _admission,
    assess_semantic_assurance_readiness,
)
from .semantic_assurance_readiness_evidence import (
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V3,
    _event_chain_for_v3,
    _source_identity,
    _v6_curator_audited_label_admission_binding,
    _v6_label_admission_binding,
)
from .semantic_assurance_readiness_metrics import (
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V5,
    SemanticAssuranceReadinessError,
    _digest,
)
from .semantic_assurance_runner_attestation import (
    SemanticAssuranceRunnerAttestationError,
    verify_semantic_assurance_runner_attestation,
)


def assess_semantic_assurance_readiness_v3(
    raw: object,
    *,
    trust_registry: Mapping[str, Any] | Path | str | None = None,
) -> dict[str, Any]:
    """Validate v2 evidence plus an independently signed runner attestation.

    The attestation is deliberately a separate gate.  A deterministic
    pre-attestation core is built from the already validated v2 receipt and
    the caller-provided source identity; only its digest is signed.  The
    returned v3 receipt is therefore not part of the signed payload and cannot
    form a circular hash dependency.  This function never authorises
    automatic semantic apply.
    """

    if not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION_V3:
        raise SemanticAssuranceReadinessError("readiness v3 evidence schema is unsupported")
    expected = {
        "schema_version", "admission_receipt", "live_receipt", "threshold_policy",
        "risk_coverage", "shadow_human_oracle", "sqlite_rehearsal",
        "architecture_invariants", "owner_charter", "source_identity",
        "runner_attestation",
    }
    if set(raw) != expected:
        raise SemanticAssuranceReadinessError("readiness v3 evidence contains unexpected fields")

    source = _source_identity(raw["source_identity"])
    # Reuse the v2 validator for all existing evidence.  It remains the
    # authoritative structural gate and is intentionally not weakened by v3.
    v2_input = {key: raw[key] for key in expected - {"schema_version", "source_identity", "runner_attestation"}}
    v2_input["schema_version"] = SCHEMA_VERSION
    try:
        v2_receipt = assess_semantic_assurance_readiness(v2_input, _allow_live_v3=True)
    except SemanticAssuranceReadinessError:
        raise

    live = raw["live_receipt"]
    if not isinstance(live, Mapping):  # Defensive; v2 already checks this.
        raise SemanticAssuranceReadinessError("readiness v3 live receipt is invalid")
    execution = live.get("execution")
    event_chain = execution.get("event_chain") if isinstance(execution, Mapping) else None
    event_chain = _event_chain_for_v3(event_chain)
    inventory = live.get("inventory")
    if not isinstance(inventory, Mapping) or set(inventory) != {"before", "after"}:
        raise SemanticAssuranceReadinessError("readiness v3 inventory binding is invalid")
    inventory_before_digest = _digest(inventory["before"])
    inventory_after_digest = _digest(inventory["after"])

    # Exclude the v2 outer digest from the pre-core.  It is itself derived
    # from the v2 core, while including it would make the signed object harder
    # to reason about and gives no additional binding strength.
    pre_core = {
        "schema_version": "bhm.semantic-assurance-pre-attestation-core.v1",
        "source_identity": source,
        "readiness_v2": {
            key: value for key, value in v2_receipt.items() if key != "readiness_digest"
        },
        "live_report_digest": live["report_digest"],
        "event_chain": event_chain,
        "inventory_before_digest": inventory_before_digest,
        "inventory_after_digest": inventory_after_digest,
    }
    pre_digest = _digest(pre_core)
    admission_receipt = raw["admission_receipt"]
    policy = raw["threshold_policy"]
    if not isinstance(admission_receipt, Mapping) or not isinstance(policy, Mapping):
        raise SemanticAssuranceReadinessError("readiness v3 admission or policy is invalid")
    corpus_admission = admission_receipt.get("corpus_admission")
    if not isinstance(corpus_admission, Mapping):
        raise SemanticAssuranceReadinessError("readiness v3 corpus admission binding is invalid")

    v2_bindings = v2_receipt["bindings"]
    expected_bindings = {
        "source_revision": source["source_revision"],
        "source_tree_sha256": source["source_tree_sha256"],
        "runner_manifest_digest": source["runner_manifest_digest"],
        "prompt_contract_digest": hashlib.sha256(PROMPT_CONTRACT.encode("utf-8")).hexdigest(),
        "corpus_admission_digest": corpus_admission.get("admission_digest"),
        "label_admission_digest": v2_bindings["admission_digest"],
        "threshold_policy_digest": v2_bindings["threshold_policy_digest"],
        "live_report_digest": live["report_digest"],
        "pre_attestation_readiness_digest": pre_digest,
        "event_count": event_chain["event_count"],
        "first_event_digest": event_chain["first_event_digest"],
        "last_event_digest": event_chain["last_event_digest"],
        "chain_root": event_chain["chain_root"],
        "inventory_before_digest": inventory_before_digest,
        "inventory_after_digest": inventory_after_digest,
    }
    try:
        runner_kwargs: dict[str, Any] = {"expected_bindings": expected_bindings}
        if trust_registry is not None:
            runner_kwargs["trust_registry"] = trust_registry
        runner = verify_semantic_assurance_runner_attestation(raw["runner_attestation"], **runner_kwargs)
    except (SemanticAssuranceRunnerAttestationError, TypeError, KeyError) as exc:
        raise SemanticAssuranceReadinessError("readiness v3 trusted runner attestation is invalid") from exc

    core = {
        "schema_version": RECEIPT_SCHEMA_VERSION_V3,
        "mode": "read-only-content-free-readiness-aggregation-v3",
        "bindings": {
            "pre_attestation_readiness_digest": pre_digest,
            "runner_attestation_digest": runner["attestation_digest"],
            "trust_registry_digest": runner["trusted_runner"]["trust_registry_digest"],
            "evaluated_run_digest": v2_receipt["bindings"]["evaluated_run_digest"],
            "admission_digest": v2_receipt["bindings"]["admission_digest"],
        },
        "evidence": v2_receipt["evidence"],
        "architecture": v2_receipt["architecture"],
        "checks": {
            **v2_receipt["checks"],
            "trusted_runner_attestation": True,
            "source_identity_binding": True,
            "event_chain_binding": True,
            "pre_attestation_no_cycle": True,
        },
        "status": "evidence_complete_but_release_not_authorized",
        "decision": {
            "automatic_apply_ready": False,
            "decision": "no_go",
            "reason": "trusted runner integrity evidence is not deployment authorization",
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
            "the trusted signature binds a bounded runner assertion, not universal semantic truth",
            "runner attestation does not prove physical HTTP execution against a compromised host",
            "automatic semantic apply remains no_go",
        ],
        "trusted_runner": runner["trusted_runner"],
    }
    return {**core, "readiness_digest": _digest(core)}


def assess_semantic_assurance_readiness_v4(
    raw: object,
    *,
    trust_registry: Mapping[str, Any] | Path | str | None = None,
) -> dict[str, Any]:
    """Validate the forward-only V6 evidence path and trusted runner proof.

    V4 deliberately accepts only human-label receipt v3 and live receipt v4.
    It carries the exact V6 corpus distribution/control bindings through the
    signed pre-attestation core without changing the legacy v2/v3 contracts.
    As with every readiness version, structural completeness is not authority
    to enable automatic semantic apply.
    """

    if not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION_V4:
        raise SemanticAssuranceReadinessError("readiness v4 evidence schema is unsupported")
    expected = {
        "schema_version", "admission_receipt", "live_receipt", "threshold_policy",
        "risk_coverage", "shadow_human_oracle", "sqlite_rehearsal",
        "architecture_invariants", "owner_charter", "source_identity",
        "runner_attestation",
    }
    if set(raw) != expected:
        raise SemanticAssuranceReadinessError("readiness v4 evidence contains unexpected fields")

    source = _source_identity(raw["source_identity"])
    admission = _admission(
        raw["admission_receipt"],
        required_receipt_schema=LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V3,
    )
    live = raw["live_receipt"]
    if not isinstance(live, Mapping) or live.get("schema_version") != LIVE_RECEIPT_SCHEMA_VERSION_V4:
        raise SemanticAssuranceReadinessError("readiness v4 requires a V6 live receipt")
    v2_input = {key: raw[key] for key in expected - {"schema_version", "source_identity", "runner_attestation"}}
    v2_input["schema_version"] = SCHEMA_VERSION
    v2_receipt = assess_semantic_assurance_readiness(
        v2_input,
        _allow_live_v4=True,
        _required_label_admission_schema=LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V3,
    )
    v6_binding = _v6_label_admission_binding(admission)
    live_binding = live.get("label_admission")
    if live_binding != v6_binding:
        raise SemanticAssuranceReadinessError("readiness v4 live V6 label binding does not match admission")
    execution = live.get("execution")
    reports = live.get("offline_reports")
    if not isinstance(reports, list) or not reports or not isinstance(reports[0], Mapping):
        raise SemanticAssuranceReadinessError("readiness v4 live reports are invalid")
    results = reports[0].get("results")
    if not isinstance(results, list):
        raise SemanticAssuranceReadinessError("readiness v4 live report results are invalid")
    expected_case_ids = {item.get("case_id") for item in results if isinstance(item, Mapping)}
    if len(expected_case_ids) != 400 or not all(isinstance(item, str) and item for item in expected_case_ids):
        raise SemanticAssuranceReadinessError("readiness v4 sealed case coverage is invalid")
    event_chain = _event_chain_for_v3(
        execution.get("event_chain") if isinstance(execution, Mapping) else None,
        expected_case_ids=expected_case_ids,
    )
    inventory = live.get("inventory")
    if not isinstance(inventory, Mapping) or set(inventory) != {"before", "after"}:
        raise SemanticAssuranceReadinessError("readiness v4 inventory binding is invalid")
    inventory_before_digest = _digest(inventory["before"])
    inventory_after_digest = _digest(inventory["after"])
    pre_core = {
        "schema_version": "bhm.semantic-assurance-pre-attestation-core.v2",
        "source_identity": source,
        "readiness_v2": {key: value for key, value in v2_receipt.items() if key != "readiness_digest"},
        "v6_label_admission": v6_binding,
        "live_report_digest": live["report_digest"],
        "event_chain": event_chain,
        "inventory_before_digest": inventory_before_digest,
        "inventory_after_digest": inventory_after_digest,
    }
    pre_digest = _digest(pre_core)
    expected_bindings = {
        "source_revision": source["source_revision"],
        "source_tree_sha256": source["source_tree_sha256"],
        "runner_manifest_digest": source["runner_manifest_digest"],
        "prompt_contract_digest": hashlib.sha256(PROMPT_CONTRACT.encode("utf-8")).hexdigest(),
        "corpus_admission_digest": v6_binding["corpus_admission_digest"],
        "label_admission_digest": v6_binding["admission_digest"],
        "threshold_policy_digest": v2_receipt["bindings"]["threshold_policy_digest"],
        "live_report_digest": live["report_digest"],
        "pre_attestation_readiness_digest": pre_digest,
        "event_count": event_chain["event_count"],
        "first_event_digest": event_chain["first_event_digest"],
        "last_event_digest": event_chain["last_event_digest"],
        "chain_root": event_chain["chain_root"],
        "inventory_before_digest": inventory_before_digest,
        "inventory_after_digest": inventory_after_digest,
    }
    try:
        runner_kwargs: dict[str, Any] = {"expected_bindings": expected_bindings}
        if trust_registry is not None:
            runner_kwargs["trust_registry"] = trust_registry
        runner = verify_semantic_assurance_runner_attestation(raw["runner_attestation"], **runner_kwargs)
    except (SemanticAssuranceRunnerAttestationError, TypeError, KeyError) as exc:
        raise SemanticAssuranceReadinessError("readiness v4 trusted runner attestation is invalid") from exc

    core = {
        "schema_version": RECEIPT_SCHEMA_VERSION_V4,
        "mode": "read-only-content-free-readiness-aggregation-v4",
        "bindings": {
            "pre_attestation_readiness_digest": pre_digest,
            "runner_attestation_digest": runner["attestation_digest"],
            "trust_registry_digest": runner["trusted_runner"]["trust_registry_digest"],
            "evaluated_run_digest": v2_receipt["bindings"]["evaluated_run_digest"],
            "admission_digest": v6_binding["admission_digest"],
            "corpus_pre_admission_digest": v6_binding["corpus_pre_admission_digest"],
            "corpus_admission_digest": v6_binding["corpus_admission_digest"],
            "admission_profile": v6_binding["admission_profile"],
            "partition_risk_counts": v6_binding["partition_risk_counts"],
            "v6_control_binding_digest": v6_binding["v6_control_binding_digest"],
        },
        "evidence": v2_receipt["evidence"],
        "architecture": v2_receipt["architecture"],
        "checks": {
            **v2_receipt["checks"],
            "v6_label_admission_binding": True,
            "v6_live_binding": True,
            "trusted_runner_attestation": True,
            "source_identity_binding": True,
            "event_chain_binding": True,
            "pre_attestation_no_cycle": True,
        },
        "status": "evidence_complete_but_release_not_authorized",
        "decision": {
            "automatic_apply_ready": False,
            "decision": "no_go",
            "reason": "V6 structural evidence and trusted runner integrity are not deployment authorization",
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
            "V6 distribution/control bindings prove declared structural continuity, not factual truth or human identity",
            "runner attestation does not prove physical HTTP execution against a compromised host",
            "automatic semantic apply remains no_go",
        ],
        "trusted_runner": runner["trusted_runner"],
    }
    return {**core, "readiness_digest": _digest(core)}


def assess_semantic_assurance_readiness_v5(
    raw: object,
    *,
    trust_registry: Mapping[str, Any] | Path | str | None = None,
) -> dict[str, Any]:
    """Validate the curator-audited forward-only V6 evidence lane.

    V5 deliberately does not upgrade a V4 package by optional fields.  It
    accepts only the exact human-label receipt v4 and live receipt v5 pair,
    then binds the verified curator-audit envelope into a separate v3
    pre-attestation core.  The runner attestation format stays v2 because it
    already signs an arbitrary bounded pre-attestation digest.
    """

    if not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION_V5:
        raise SemanticAssuranceReadinessError("readiness v5 evidence schema is unsupported")
    expected = {
        "schema_version", "admission_receipt", "live_receipt", "threshold_policy",
        "risk_coverage", "shadow_human_oracle", "sqlite_rehearsal",
        "architecture_invariants", "owner_charter", "source_identity",
        "runner_attestation",
    }
    if set(raw) != expected:
        raise SemanticAssuranceReadinessError("readiness v5 evidence contains unexpected fields")

    source = _source_identity(raw["source_identity"])
    admission = _admission(
        raw["admission_receipt"],
        required_receipt_schema=LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V5,
    )
    curator_binding = _v6_curator_audited_label_admission_binding(admission)
    live = raw["live_receipt"]
    if not isinstance(live, Mapping) or live.get("schema_version") != LIVE_RECEIPT_SCHEMA_VERSION_V5:
        raise SemanticAssuranceReadinessError("readiness v5 requires a curator-audited V6 live receipt")

    v2_input = {key: raw[key] for key in expected - {"schema_version", "source_identity", "runner_attestation"}}
    v2_input["schema_version"] = SCHEMA_VERSION
    v2_receipt = assess_semantic_assurance_readiness(
        v2_input,
        _allow_live_v5=True,
        _required_label_admission_schema=LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V5,
    )
    if live.get("label_admission") != curator_binding:
        raise SemanticAssuranceReadinessError("readiness v5 live curator binding does not match admission")

    execution = live.get("execution")
    reports = live.get("offline_reports")
    if not isinstance(reports, list) or not reports or not isinstance(reports[0], Mapping):
        raise SemanticAssuranceReadinessError("readiness v5 live reports are invalid")
    results = reports[0].get("results")
    if not isinstance(results, list):
        raise SemanticAssuranceReadinessError("readiness v5 live report results are invalid")
    expected_case_ids = {item.get("case_id") for item in results if isinstance(item, Mapping)}
    if len(expected_case_ids) != 400 or not all(isinstance(item, str) and item for item in expected_case_ids):
        raise SemanticAssuranceReadinessError("readiness v5 sealed case coverage is invalid")
    event_chain = _event_chain_for_v3(
        execution.get("event_chain") if isinstance(execution, Mapping) else None,
        expected_case_ids=expected_case_ids,
    )
    inventory = live.get("inventory")
    if not isinstance(inventory, Mapping) or set(inventory) != {"before", "after"}:
        raise SemanticAssuranceReadinessError("readiness v5 inventory binding is invalid")
    inventory_before_digest = _digest(inventory["before"])
    inventory_after_digest = _digest(inventory["after"])
    pre_core = {
        "schema_version": "bhm.semantic-assurance-pre-attestation-core.v3",
        "source_identity": source,
        "readiness_v2": {key: value for key, value in v2_receipt.items() if key != "readiness_digest"},
        "v6_curator_audited_label_admission": curator_binding,
        "live_report_digest": live["report_digest"],
        "event_chain": event_chain,
        "inventory_before_digest": inventory_before_digest,
        "inventory_after_digest": inventory_after_digest,
    }
    pre_digest = _digest(pre_core)
    expected_bindings = {
        "source_revision": source["source_revision"],
        "source_tree_sha256": source["source_tree_sha256"],
        "runner_manifest_digest": source["runner_manifest_digest"],
        "prompt_contract_digest": hashlib.sha256(PROMPT_CONTRACT.encode("utf-8")).hexdigest(),
        "corpus_admission_digest": curator_binding["corpus_admission_digest"],
        "label_admission_digest": curator_binding["admission_digest"],
        "threshold_policy_digest": v2_receipt["bindings"]["threshold_policy_digest"],
        "live_report_digest": live["report_digest"],
        "pre_attestation_readiness_digest": pre_digest,
        "event_count": event_chain["event_count"],
        "first_event_digest": event_chain["first_event_digest"],
        "last_event_digest": event_chain["last_event_digest"],
        "chain_root": event_chain["chain_root"],
        "inventory_before_digest": inventory_before_digest,
        "inventory_after_digest": inventory_after_digest,
    }
    try:
        runner_kwargs: dict[str, Any] = {"expected_bindings": expected_bindings}
        if trust_registry is not None:
            runner_kwargs["trust_registry"] = trust_registry
        runner = verify_semantic_assurance_runner_attestation(raw["runner_attestation"], **runner_kwargs)
    except (SemanticAssuranceRunnerAttestationError, TypeError, KeyError) as exc:
        raise SemanticAssuranceReadinessError("readiness v5 trusted runner attestation is invalid") from exc

    core = {
        "schema_version": RECEIPT_SCHEMA_VERSION_V5,
        "mode": "read-only-content-free-readiness-aggregation-v5",
        "bindings": {
            "pre_attestation_readiness_digest": pre_digest,
            "runner_attestation_digest": runner["attestation_digest"],
            "trust_registry_digest": runner["trusted_runner"]["trust_registry_digest"],
            "evaluated_run_digest": v2_receipt["bindings"]["evaluated_run_digest"],
            "admission_digest": curator_binding["admission_digest"],
            "corpus_pre_admission_digest": curator_binding["corpus_pre_admission_digest"],
            "corpus_admission_digest": curator_binding["corpus_admission_digest"],
            "admission_profile": curator_binding["admission_profile"],
            "partition_risk_counts": curator_binding["partition_risk_counts"],
            "v6_control_binding_digest": curator_binding["v6_control_binding_digest"],
            "curator_audit": curator_binding["curator_audit"],
        },
        "evidence": v2_receipt["evidence"],
        "architecture": v2_receipt["architecture"],
        "checks": {
            **v2_receipt["checks"],
            "v6_curator_audit_binding": True,
            "v6_live_binding": True,
            "trusted_runner_attestation": True,
            "source_identity_binding": True,
            "event_chain_binding": True,
            "pre_attestation_no_cycle": True,
        },
        "status": "evidence_complete_but_release_not_authorized",
        "decision": {
            "automatic_apply_ready": False,
            "decision": "no_go",
            "reason": "curator-audited V6 structural evidence is not deployment authorization",
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
            "curator-audit bindings do not prove factual truth, human identity, or semantic independence",
            "runner attestation does not prove physical HTTP execution against a compromised host",
            "automatic semantic apply remains no_go",
        ],
        "trusted_runner": runner["trusted_runner"],
    }
    return {**core, "readiness_digest": _digest(core)}


__all__ = [
    "assess_semantic_assurance_readiness_v3",
    "assess_semantic_assurance_readiness_v4",
    "assess_semantic_assurance_readiness_v5",
]
