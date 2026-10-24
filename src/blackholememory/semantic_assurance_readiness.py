"""Fail-closed aggregation of bounded semantic-assurance evidence.

This module is deliberately a *reader* of local, content-free receipts.  It
does not contact a model, start a runtime, open BHM storage, or make a
lifecycle decision.  A mechanically complete result proves only that the
declared finite evidence package is structurally bound.  It is never an
authorisation to enable automatic semantic apply.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .semantic_assurance_label_admission import (
    SemanticAssuranceLabelAdmissionError,
    verify_semantic_assurance_label_admission,
)
from .semantic_assurance_live import (
    LIVE_RECEIPT_SCHEMA_VERSION_V4,
    LIVE_RECEIPT_SCHEMA_VERSION_V5,
    PROMPT_CONTRACT,
)
from .semantic_assurance_readiness_evidence import (
    _V6_ADMISSION_PROFILE as _V6_ADMISSION_PROFILE,  # noqa: F401
)
from .semantic_assurance_readiness_evidence import (
    _V6_CURATOR_AUDITED_ADMISSION_PROFILE as _V6_CURATOR_AUDITED_ADMISSION_PROFILE,  # noqa: F401
)
from .semantic_assurance_readiness_evidence import (
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V3,
    _event_chain_for_v3,
    _live_receipt,
    _source_identity,
    _v6_curator_audited_label_admission_binding,
    _v6_label_admission_binding,
    _v6_private_public_live,
)
from .semantic_assurance_readiness_evidence import (
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V4 as LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V4,  # noqa: F401
)
from .semantic_assurance_readiness_evidence import (
    LIVE_RECEIPT_SCHEMA_VERSION as LIVE_RECEIPT_SCHEMA_VERSION,  # noqa: F401
)
from .semantic_assurance_readiness_evidence import (
    _live_execution as _live_execution,  # noqa: F401
)
from .semantic_assurance_readiness_evidence import (
    _live_inventory as _live_inventory,  # noqa: F401
)
from .semantic_assurance_readiness_evidence import (
    _v6_curator_audited_live_label_admission as _v6_curator_audited_live_label_admission,  # noqa: F401
)
from .semantic_assurance_readiness_evidence import (
    _v6_live_label_admission as _v6_live_label_admission,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _DIGEST_LENGTH as _DIGEST_LENGTH,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _MAX_INPUT_BYTES,
    _REQUIRED_RISK_CLASSES,
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V5,
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V6,
    SemanticAssuranceReadinessError,
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
from .semantic_assurance_readiness_metrics import (
    _MINIMUM_SEALED_CASE_COUNT as _MINIMUM_SEALED_CASE_COUNT,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _MINIMUM_SEALED_SUPPORT_ROW_COUNT as _MINIMUM_SEALED_SUPPORT_ROW_COUNT,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _MINIMUM_STRATUM_SUPPORT as _MINIMUM_STRATUM_SUPPORT,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _MINIMUM_WILSON_LOWER_95 as _MINIMUM_WILSON_LOWER_95,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _REQUIRED_SEALED_REPEATS as _REQUIRED_SEALED_REPEATS,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _REQUIRED_STRATIFIED_LABELS as _REQUIRED_STRATIFIED_LABELS,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _SEALED_CRITICAL_RISK_CLASSES as _SEALED_CRITICAL_RISK_CLASSES,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _V6_SEALED_CONFLICT_CLASSES as _V6_SEALED_CONFLICT_CLASSES,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _metrics as _metrics,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _nonnegative_int as _nonnegative_int,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _offline_report as _offline_report,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _positive_int as _positive_int,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _rate as _rate,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _risk_list as _risk_list,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _risk_stratified_metrics as _risk_stratified_metrics,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _stratified_metrics as _stratified_metrics,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _stratified_policy_map as _stratified_policy_map,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _text as _text,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    _timestamp as _timestamp,  # noqa: F401
)
from .semantic_assurance_runner_attestation import (
    SemanticAssuranceRunnerAttestationError,
    verify_semantic_assurance_runner_attestation,
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


def load_semantic_assurance_readiness(path: Path | str) -> dict[str, Any]:
    """Read one bounded local JSON package and emit its public-safe receipt."""

    source = Path(path)
    try:
        if source.stat().st_size > _MAX_INPUT_BYTES:
            raise SemanticAssuranceReadinessError("readiness evidence package exceeds the bounded size")
        raw = json.loads(source.read_text(encoding="utf-8"))
    except SemanticAssuranceReadinessError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticAssuranceReadinessError("readiness evidence package is unreadable") from exc
    if isinstance(raw, Mapping) and raw.get("schema_version") == SCHEMA_VERSION_V3:
        return assess_semantic_assurance_readiness_v3(raw)
    if isinstance(raw, Mapping) and raw.get("schema_version") == SCHEMA_VERSION_V4:
        return assess_semantic_assurance_readiness_v4(raw)
    if isinstance(raw, Mapping) and raw.get("schema_version") == SCHEMA_VERSION_V5:
        return assess_semantic_assurance_readiness_v5(raw)
    if isinstance(raw, Mapping) and raw.get("schema_version") == SCHEMA_VERSION_V6:
        return assess_semantic_assurance_readiness_v6(raw)
    if isinstance(raw, Mapping) and raw.get("schema_version") == SCHEMA_VERSION_V7:
        return assess_semantic_assurance_readiness_v7(raw)
    return assess_semantic_assurance_readiness(raw)


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
    "assess_semantic_assurance_readiness",
    "assess_semantic_assurance_readiness_v3",
    "assess_semantic_assurance_readiness_v4",
    "assess_semantic_assurance_readiness_v5",
    "assess_semantic_assurance_readiness_v6",
    "assess_semantic_assurance_readiness_v7",
    "load_semantic_assurance_readiness",
    "write_semantic_assurance_readiness_receipt",
]
