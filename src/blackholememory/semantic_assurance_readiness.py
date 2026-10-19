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
import math
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .semantic_assurance_label_admission import (
    SemanticAssuranceLabelAdmissionError,
    verify_semantic_assurance_label_admission,
)
from .semantic_assurance_evaluation import recompute_semantic_assurance_results, wilson_interval
from .semantic_assurance_live import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_VARIANT,
    LIVE_RECEIPT_SCHEMA_VERSION as LIVE_RECEIPT_SCHEMA_VERSION_V3,
    LIVE_RECEIPT_SCHEMA_VERSION_V4,
    LIVE_RECEIPT_SCHEMA_VERSION_V5,
    PROMPT_CONTRACT,
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
LIVE_RECEIPT_SCHEMA_VERSION = "bhm.semantic-assurance-live-receipt.v1"
LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V2 = "bhm.semantic-assurance-human-label-admission-receipt.v2"
LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V3 = "bhm.semantic-assurance-human-label-admission-receipt.v3"
LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V4 = "bhm.semantic-assurance-human-label-admission-receipt.v4"
_V6_ADMISSION_PROFILE = "v6_independent_human_curation"
_V6_CURATOR_AUDITED_ADMISSION_PROFILE = "v6_curator_audited_independent_human_curation"
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
        required_receipt_schema=LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V4,
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
        _required_label_admission_schema=LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V4,
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


def _v6_label_admission_binding(admission: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only the V6 receipt fields that must survive to readiness v4."""

    if admission.get("schema_version") != LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V3:
        raise SemanticAssuranceReadinessError("readiness v4 label admission is not V6")
    corpus = admission.get("corpus")
    corpus_admission = admission.get("corpus_admission")
    assurance = admission.get("assurance")
    expected_corpus_keys = {
        "receipt_schema_version", "admission_digest", "case_count", "partition_counts",
        "sealed_support_count", "dataset_binding_digest", "sealed_case_binding_digest",
        "sealed_support_binding_digest", "risk_counts", "sealed_risk_counts",
        "packet_payload_set_digest", "curator_ledger_digest", "curator_attestation_digest",
        "admission_profile", "partition_risk_counts", "v6_control_binding_digest",
    }
    if (
        not isinstance(corpus, Mapping)
        or not isinstance(corpus_admission, Mapping)
        or not isinstance(assurance, Mapping)
        or set(corpus_admission) != expected_corpus_keys
        or corpus_admission.get("receipt_schema_version") != "bhm.semantic-assurance-corpus-admission-receipt.v2"
        or corpus_admission.get("admission_profile") != _V6_ADMISSION_PROFILE
        or assurance.get("v6_corpus_structurally_bound") is not True
    ):
        raise SemanticAssuranceReadinessError("readiness v4 V6 corpus admission binding is invalid")
    corpus_admission_digest = _digest_text(corpus_admission.get("admission_digest"), "readiness v4 corpus admission digest")
    if _digest_text(corpus.get("corpus_pre_admission_digest"), "readiness v4 corpus pre-admission digest") != corpus_admission_digest:
        raise SemanticAssuranceReadinessError("readiness v4 corpus pre-admission link is invalid")
    partition_counts = corpus_admission.get("partition_counts")
    partition_risk_counts = corpus_admission.get("partition_risk_counts")
    if (
        not isinstance(partition_counts, Mapping)
        or set(partition_counts) != {"development", "validation", "sealed_holdout"}
        or any(
            not isinstance(partition_counts[partition], int)
            or isinstance(partition_counts[partition], bool)
            or partition_counts[partition] < minimum
            for partition, minimum in (("development", 220), ("validation", 220), ("sealed_holdout", 400))
        )
        or not isinstance(partition_risk_counts, Mapping)
        or set(partition_risk_counts) != {"development", "validation", "sealed_holdout"}
    ):
        raise SemanticAssuranceReadinessError("readiness v4 V6 partition distribution is invalid")
    normalized_risk_counts: dict[str, dict[str, int]] = {}
    for partition in ("development", "validation", "sealed_holdout"):
        row = partition_risk_counts.get(partition)
        if (
            not isinstance(row, Mapping)
            or set(row) != _REQUIRED_RISK_CLASSES
            or any(not isinstance(row[risk], int) or isinstance(row[risk], bool) or row[risk] < 1 for risk in _REQUIRED_RISK_CLASSES)
            or sum(row.values()) != partition_counts[partition]
        ):
            raise SemanticAssuranceReadinessError("readiness v4 V6 partition risk distribution is invalid")
        if partition == "sealed_holdout" and (
            any(row[risk] < 50 for risk in _V6_SEALED_CONFLICT_CLASSES)
            or sum(row[risk] for risk in _V6_SEALED_CONFLICT_CLASSES) < 200
        ):
            raise SemanticAssuranceReadinessError("readiness v4 V6 sealed conflict distribution is invalid")
        normalized_risk_counts[partition] = {risk: row[risk] for risk in sorted(_REQUIRED_RISK_CLASSES)}
    return {
        "receipt_schema_version": LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V3,
        "admission_digest": _digest_text(admission.get("admission_digest"), "readiness v4 label admission digest"),
        "corpus_pre_admission_digest": corpus_admission_digest,
        "corpus_admission_digest": corpus_admission_digest,
        "admission_profile": _V6_ADMISSION_PROFILE,
        "partition_risk_counts": normalized_risk_counts,
        "v6_control_binding_digest": _digest_text(
            corpus_admission.get("v6_control_binding_digest"),
            "readiness v4 V6 control binding digest",
        ),
    }


def _v6_curator_audited_label_admission_binding(admission: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the exact curator-audited binding reserved for readiness v5."""

    if admission.get("schema_version") != LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V4:
        raise SemanticAssuranceReadinessError("readiness v5 label admission is not curator-audited V6")
    corpus = admission.get("corpus")
    corpus_admission = admission.get("corpus_admission")
    assurance = admission.get("assurance")
    expected_corpus_keys = {
        "receipt_schema_version", "admission_digest", "case_count", "partition_counts",
        "sealed_support_count", "dataset_binding_digest", "sealed_case_binding_digest",
        "sealed_support_binding_digest", "risk_counts", "sealed_risk_counts",
        "packet_payload_set_digest", "curator_ledger_digest", "curator_attestation_digest",
        "admission_profile", "partition_risk_counts", "v6_control_binding_digest",
        "curator_audit",
    }
    expected_audit_keys = {
        "receipt_schema_version", "audit_digest", "atomic_fact_binding_digest",
        "control_binding_digest", "canonical_fragment_binding_digest",
    }
    if (
        not isinstance(corpus, Mapping)
        or not isinstance(corpus_admission, Mapping)
        or not isinstance(assurance, Mapping)
        or set(corpus_admission) != expected_corpus_keys
        or corpus_admission.get("receipt_schema_version") != "bhm.semantic-assurance-corpus-admission-receipt.v3"
        or corpus_admission.get("admission_profile") != _V6_CURATOR_AUDITED_ADMISSION_PROFILE
        or assurance.get("v6_corpus_structurally_bound") is not True
        or assurance.get("v6_curator_audit_exactly_bound") is not True
    ):
        raise SemanticAssuranceReadinessError("readiness v5 curator-audited corpus admission binding is invalid")
    corpus_admission_digest = _digest_text(corpus_admission.get("admission_digest"), "readiness v5 corpus admission digest")
    if _digest_text(corpus.get("corpus_pre_admission_digest"), "readiness v5 corpus pre-admission digest") != corpus_admission_digest:
        raise SemanticAssuranceReadinessError("readiness v5 corpus pre-admission link is invalid")
    partition_counts = corpus_admission.get("partition_counts")
    partition_risk_counts = corpus_admission.get("partition_risk_counts")
    if (
        not isinstance(partition_counts, Mapping)
        or set(partition_counts) != {"development", "validation", "sealed_holdout"}
        or any(
            not isinstance(partition_counts[partition], int)
            or isinstance(partition_counts[partition], bool)
            or partition_counts[partition] < minimum
            for partition, minimum in (("development", 220), ("validation", 220), ("sealed_holdout", 400))
        )
        or not isinstance(partition_risk_counts, Mapping)
        or set(partition_risk_counts) != {"development", "validation", "sealed_holdout"}
    ):
        raise SemanticAssuranceReadinessError("readiness v5 V6 partition distribution is invalid")
    normalized_risk_counts: dict[str, dict[str, int]] = {}
    for partition in ("development", "validation", "sealed_holdout"):
        row = partition_risk_counts.get(partition)
        if (
            not isinstance(row, Mapping)
            or set(row) != _REQUIRED_RISK_CLASSES
            or any(not isinstance(row[risk], int) or isinstance(row[risk], bool) or row[risk] < 1 for risk in _REQUIRED_RISK_CLASSES)
            or sum(row.values()) != partition_counts[partition]
        ):
            raise SemanticAssuranceReadinessError("readiness v5 V6 partition risk distribution is invalid")
        if partition == "sealed_holdout" and (
            any(row[risk] < 50 for risk in _V6_SEALED_CONFLICT_CLASSES)
            or sum(row[risk] for risk in _V6_SEALED_CONFLICT_CLASSES) < 200
        ):
            raise SemanticAssuranceReadinessError("readiness v5 V6 sealed conflict distribution is invalid")
        normalized_risk_counts[partition] = {risk: row[risk] for risk in sorted(_REQUIRED_RISK_CLASSES)}
    audit = corpus_admission.get("curator_audit")
    if (
        not isinstance(audit, Mapping)
        or set(audit) != expected_audit_keys
        or audit.get("receipt_schema_version") != "bhm.semantic-assurance-curator-audit-receipt.v1"
    ):
        raise SemanticAssuranceReadinessError("readiness v5 curator audit binding is invalid")
    normalized_audit = {
        "receipt_schema_version": "bhm.semantic-assurance-curator-audit-receipt.v1",
        **{
            key: _digest_text(audit.get(key), f"readiness v5 curator audit {key}")
            for key in sorted(expected_audit_keys - {"receipt_schema_version"})
        },
    }
    return {
        "receipt_schema_version": LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V4,
        "admission_digest": _digest_text(admission.get("admission_digest"), "readiness v5 label admission digest"),
        "corpus_pre_admission_digest": corpus_admission_digest,
        "corpus_admission_digest": corpus_admission_digest,
        "admission_profile": _V6_CURATOR_AUDITED_ADMISSION_PROFILE,
        "partition_risk_counts": normalized_risk_counts,
        "v6_control_binding_digest": _digest_text(
            corpus_admission.get("v6_control_binding_digest"),
            "readiness v5 V6 control binding digest",
        ),
        "curator_audit": normalized_audit,
    }


def _v6_live_label_admission(value: object, admission: Mapping[str, Any]) -> dict[str, Any]:
    expected = _v6_label_admission_binding(admission)
    if value != expected:
        raise SemanticAssuranceReadinessError("readiness v4 live V6 label binding is invalid")
    return expected


def _v6_curator_audited_live_label_admission(value: object, admission: Mapping[str, Any]) -> dict[str, Any]:
    expected = _v6_curator_audited_label_admission_binding(admission)
    if value != expected:
        raise SemanticAssuranceReadinessError("readiness v5 live curator binding is invalid")
    return expected


def _source_identity(value: object) -> dict[str, str]:
    """Validate the source/runner manifest binding used by readiness v3."""

    expected = {"schema_version", "source_revision", "source_tree_sha256", "runner_manifest_digest"}
    if not isinstance(value, Mapping) or set(value) != expected or value.get("schema_version") != "bhm.semantic-assurance-source-identity.v1":
        raise SemanticAssuranceReadinessError("readiness v3 source identity is invalid")
    revision = value.get("source_revision")
    if not isinstance(revision, str) or len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise SemanticAssuranceReadinessError("readiness v3 source revision is invalid")
    return {
        "schema_version": "bhm.semantic-assurance-source-identity.v1",
        "source_revision": revision,
        "source_tree_sha256": _digest_text(value.get("source_tree_sha256"), "readiness v3 source tree digest"),
        "runner_manifest_digest": _digest_text(value.get("runner_manifest_digest"), "readiness v3 runner manifest digest"),
    }


def _event_chain_for_v3(
    value: object,
    *,
    expected_case_ids: set[str] | None = None,
    expected_inventory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the content-free 1200-event chain summary.

    Individual request/response bodies never enter receipts.  If a trusted
    runner emits a private event log, only its independently computed anchors
    are published here and signed by the runner attestation.
    """

    expected = {
        "events", "event_count", "first_event_digest", "last_event_digest", "chain_root",
        "inventory_before_digest", "inventory_after_digest",
    }
    event_expected = {
        "sequence", "repeat_number", "case_id", "request_digest", "response_digest", "event_digest",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or value.get("event_count") != 1200
        or not isinstance(value.get("events"), list)
        or len(value["events"]) != 1200
    ):
        raise SemanticAssuranceReadinessError("readiness v3 event chain is invalid")
    before_digest = _digest_text(value.get("inventory_before_digest"), "readiness v3 inventory before digest")
    after_digest = _digest_text(value.get("inventory_after_digest"), "readiness v3 inventory after digest")
    if expected_inventory is not None and (
        before_digest != _digest(expected_inventory.get("before"))
        or after_digest != _digest(expected_inventory.get("after"))
    ):
        raise SemanticAssuranceReadinessError("readiness v3 event chain inventory binding is invalid")
    previous = _digest({
        "domain": "bhm.semantic-assurance-live-event-chain.v3",
        "inventory_before_digest": before_digest,
    })
    pairs: set[tuple[int, str]] = set()
    for sequence, event in enumerate(value["events"], start=1):
        if (
            not isinstance(event, Mapping)
            or set(event) != event_expected
            or event.get("sequence") != sequence
            or event.get("repeat_number") not in {1, 2, 3}
            or not isinstance(event.get("case_id"), str)
            or not event["case_id"]
        ):
            raise SemanticAssuranceReadinessError("readiness v3 event chain is invalid")
        request_digest = _digest_text(event.get("request_digest"), "readiness v3 event request digest")
        response_digest = _digest_text(event.get("response_digest"), "readiness v3 event response digest")
        event_digest = _digest_text(event.get("event_digest"), "readiness v3 event digest")
        if event_digest != _digest({
            "sequence": sequence,
            "repeat_number": event["repeat_number"],
            "case_id": event["case_id"],
            "request_digest": request_digest,
            "response_digest": response_digest,
            "previous_event_digest": previous,
        }):
            raise SemanticAssuranceReadinessError("readiness v3 event chain digest is invalid")
        pair = (event["repeat_number"], event["case_id"])
        if pair in pairs:
            raise SemanticAssuranceReadinessError("readiness v3 event chain coverage is invalid")
        pairs.add(pair)
        previous = event_digest
    first = _digest_text(value.get("first_event_digest"), "readiness v3 first event digest")
    last = _digest_text(value.get("last_event_digest"), "readiness v3 last event digest")
    root = _digest_text(value.get("chain_root"), "readiness v3 chain root")
    if first != value["events"][0]["event_digest"] or last != value["events"][-1]["event_digest"]:
        raise SemanticAssuranceReadinessError("readiness v3 event chain anchors are invalid")
    if root != _digest({
        "domain": "bhm.semantic-assurance-live-event-chain.v3",
        "inventory_before_digest": before_digest,
        "inventory_after_digest": after_digest,
        "event_count": 1200,
        "first_event_digest": first,
        "last_event_digest": last,
    }):
        raise SemanticAssuranceReadinessError("readiness v3 event chain root is invalid")
    if expected_case_ids is not None:
        expected_pairs = {(repeat, case_id) for repeat in (1, 2, 3) for case_id in expected_case_ids}
        if len(expected_case_ids) != 400 or pairs != expected_pairs:
            raise SemanticAssuranceReadinessError("readiness v3 event chain coverage is invalid")
    return {
        "event_count": 1200,
        "first_event_digest": first,
        "last_event_digest": last,
        "chain_root": root,
        "inventory_before_digest": before_digest,
        "inventory_after_digest": after_digest,
    }


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


def _live_receipt(
    value: object,
    admission: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    allow_live_v3: bool = False,
    allow_live_v4: bool = False,
    allow_live_v5: bool = False,
) -> dict[str, Any]:
    expected = {
        "schema_version", "mode", "dataset", "model", "prompt_contract", "execution", "inventory",
        "model_load_events", "model_load_telemetry", "offline_reports", "failure", "gate", "limitations", "report_digest",
    }
    schema_version = value.get("schema_version") if isinstance(value, Mapping) else None
    if schema_version == LIVE_RECEIPT_SCHEMA_VERSION_V3 and not allow_live_v3:
        raise SemanticAssuranceReadinessError("readiness live receipt is invalid")
    if schema_version == LIVE_RECEIPT_SCHEMA_VERSION_V4 and not allow_live_v4:
        raise SemanticAssuranceReadinessError("readiness live receipt is invalid")
    if schema_version == LIVE_RECEIPT_SCHEMA_VERSION_V5 and not allow_live_v5:
        raise SemanticAssuranceReadinessError("readiness live receipt is invalid")
    if schema_version not in {
        LIVE_RECEIPT_SCHEMA_VERSION,
        LIVE_RECEIPT_SCHEMA_VERSION_V3,
        LIVE_RECEIPT_SCHEMA_VERSION_V4,
        LIVE_RECEIPT_SCHEMA_VERSION_V5,
    }:
        raise SemanticAssuranceReadinessError("readiness live receipt is invalid")
    if schema_version in {LIVE_RECEIPT_SCHEMA_VERSION_V4, LIVE_RECEIPT_SCHEMA_VERSION_V5}:
        expected.add("label_admission")
    if not isinstance(value, Mapping) or set(value) != expected or value.get("mode") != "live-read-only-local-classifier":
        raise SemanticAssuranceReadinessError("readiness live receipt is invalid")
    core = {key: value[key] for key in expected - {"report_digest"}}
    if _digest(core) != _digest_text(value.get("report_digest"), "live receipt digest"):
        raise SemanticAssuranceReadinessError("readiness live receipt digest mismatch")
    dataset = value.get("dataset")
    admitted_dataset = {key: admission["corpus"].get(key) for key in ("dataset_id", "fixture_digest", "label_digest")}
    if not isinstance(dataset, Mapping) or set(dataset) != {"dataset_id", "fixture_digest", "label_digest"} or dict(dataset) != admitted_dataset:
        raise SemanticAssuranceReadinessError("readiness live receipt does not bind the admitted corpus")
    model = value.get("model")
    prompt = value.get("prompt_contract")
    if not isinstance(model, Mapping) or set(model) != {"model_id", "model_variant", "endpoint"} or not isinstance(prompt, Mapping) or set(prompt) != {"name", "digest"}:
        raise SemanticAssuranceReadinessError("readiness live model identity is invalid")
    model_binding = {key: _text(model.get(key), f"live model {key}", 512) for key in sorted(model)}
    if model_binding != {"endpoint": DEFAULT_BASE_URL, "model_id": DEFAULT_MODEL_ID, "model_variant": DEFAULT_MODEL_VARIANT}:
        raise SemanticAssuranceReadinessError("readiness live model identity is not the frozen local model")
    prompt_digest = _digest_text(prompt.get("digest"), "live prompt digest")
    if not _text(prompt.get("name"), "live prompt name", 160):
        raise SemanticAssuranceReadinessError("readiness live prompt identity is invalid")
    if prompt.get("name") != PROMPT_CONTRACT or prompt_digest != _digest(PROMPT_CONTRACT):
        raise SemanticAssuranceReadinessError("readiness live prompt contract is not frozen")
    execution = _live_execution(
        value.get("execution"),
        require_event_chain=schema_version in {LIVE_RECEIPT_SCHEMA_VERSION_V3, LIVE_RECEIPT_SCHEMA_VERSION_V4, LIVE_RECEIPT_SCHEMA_VERSION_V5},
    )
    _live_inventory(value.get("inventory"))
    if value.get("failure") is not None or value.get("model_load_events") != 0 or value.get("model_load_telemetry") != "available":
        raise SemanticAssuranceReadinessError("readiness live execution is incomplete or has unsafe model-load evidence")
    reports = value.get("offline_reports")
    if not isinstance(reports, list) or len(reports) != execution["repeats_completed"] or len(reports) != execution["repeats_requested"] or not reports:
        raise SemanticAssuranceReadinessError("readiness live receipt repeat coverage is invalid")
    v6_label_admission = (
        _v6_live_label_admission(value.get("label_admission"), admission)
        if schema_version == LIVE_RECEIPT_SCHEMA_VERSION_V4
        else _v6_curator_audited_live_label_admission(value.get("label_admission"), admission)
        if schema_version == LIVE_RECEIPT_SCHEMA_VERSION_V5 else None
    )
    report_summaries = [
        _offline_report(item, admission, dataset, model_binding, prompt_digest, v6_label_admission=v6_label_admission)
        for item in reports
    ]
    prediction_digests = {item["prediction_digest"] for item in report_summaries}
    if len(prediction_digests) != 1 or len({item["results_digest"] for item in report_summaries}) != 1:
        raise SemanticAssuranceReadinessError("readiness live receipt lacks exact deterministic result agreement")
    repeat_ledger = execution["repeat_executions"]
    expected_case_binding = admission["corpus_admission"].get("sealed_case_binding_digest")
    if [entry.get("repeat_number") for entry in repeat_ledger] != [1, 2, 3]:
        raise SemanticAssuranceReadinessError("readiness live repeat execution numbering is invalid")
    for entry, summary, report in zip(repeat_ledger, report_summaries, reports, strict=True):
        if set(entry) != {"repeat_number", "case_count", "case_id_set_digest", "chat_call_count", "prediction_digest", "execution_digest"} or _digest({key: entry[key] for key in entry if key != "execution_digest"}) != entry["execution_digest"]:
            raise SemanticAssuranceReadinessError("readiness live repeat execution digest is invalid")
        if entry["case_count"] != summary["counts"]["case_count"] or entry["prediction_digest"] != summary["prediction_digest"] or entry["case_id_set_digest"] != summary["case_id_set_digest"] or (expected_case_binding and entry["case_id_set_digest"] != expected_case_binding) or entry["chat_call_count"] != summary["counts"]["case_count"]:
            raise SemanticAssuranceReadinessError("readiness live repeat execution does not bind report coverage")
        invocation = report["invocation"]
        if invocation.get("repeat_number") != str(entry["repeat_number"]) or invocation.get("case_count") != str(entry["case_count"]) or invocation.get("case_id_set_digest") != entry["case_id_set_digest"]:
            raise SemanticAssuranceReadinessError("readiness live repeat invocation binding is invalid")
    if execution["chat_call_count"] != sum(entry["chat_call_count"] for entry in repeat_ledger) or execution["chat_call_count"] != summary["counts"]["case_count"] * 3:
        raise SemanticAssuranceReadinessError("readiness live chat call coverage is not exact")
    gate = value.get("gate")
    expected_checks = {
        "exact_requested_model_loaded_before": True, "exact_requested_model_loaded_after": True,
        "only_requested_llm_loaded_before": True, "only_requested_llm_loaded_after": True,
        "all_classifications_parsed": True, "offline_mechanical_valid": True,
        "deterministic_repeat_agreement": True, "exact_repeat_execution_coverage": True, "model_boundaries_intact": True,
    }
    if schema_version in {LIVE_RECEIPT_SCHEMA_VERSION_V3, LIVE_RECEIPT_SCHEMA_VERSION_V4, LIVE_RECEIPT_SCHEMA_VERSION_V5}:
        expected_checks["content_free_event_chain_integrity"] = True
    if schema_version in {LIVE_RECEIPT_SCHEMA_VERSION_V4, LIVE_RECEIPT_SCHEMA_VERSION_V5}:
        expected_checks["v6_label_admission_bound"] = True
    if not isinstance(gate, Mapping) or set(gate) != {"checks", "mechanical_valid", "accepted", "acceptance_scope", "automatic_apply_ready", "automatic_apply_decision"} or gate.get("checks") != expected_checks or gate.get("mechanical_valid") is not True or gate.get("accepted") is not True or gate.get("automatic_apply_ready") is not False or gate.get("automatic_apply_decision") != "no_go":
        raise SemanticAssuranceReadinessError("readiness live gate is invalid")
    summary = report_summaries[0]
    if summary["quality_gate"]["automatic_apply_decision"] != "no_go":
        raise SemanticAssuranceReadinessError("readiness live report safety boundary is invalid")
    evaluated_run_digest = _digest({"live_report_digest": value["report_digest"], "report_digests": [item["report_digest"] for item in report_summaries], "prediction_digest": summary["prediction_digest"], "results_digest": summary["results_digest"], "admission_digest": admission["admission_digest"]})
    model_run_identity_digest = _digest({"model": model_binding, "prompt_contract": dict(prompt), "partition": execution["partition"], "repeats": execution["repeats_completed"], "chat_call_count": execution["chat_call_count"]})
    event_chain: dict[str, Any] | None = None
    if schema_version in {LIVE_RECEIPT_SCHEMA_VERSION_V3, LIVE_RECEIPT_SCHEMA_VERSION_V4, LIVE_RECEIPT_SCHEMA_VERSION_V5}:
        event_chain = _event_chain_for_v3(
            execution["event_chain"],
            expected_case_ids=set(report_summaries[0]["case_ids"]),
            expected_inventory=value["inventory"],
        )
    return {
        "evaluated_run_digest": evaluated_run_digest,
        "model_run_identity_digest": model_run_identity_digest,
        "dataset_binding_digest": _digest(dict(dataset)),
        "prompt_contract_digest": prompt_digest,
        "counts": summary["counts"],
        "metrics": summary["metrics"],
        "stratified_metrics": summary["stratified_metrics"],
        "repeat": {"requested": execution["repeats_requested"], "completed": execution["repeats_completed"]},
        "live_report_digest": _digest_text(value["report_digest"], "live receipt digest"),
        "event_chain": event_chain,
        "v6_label_admission": v6_label_admission,
    }


def _live_execution(value: object, *, require_event_chain: bool = False) -> dict[str, Any]:
    expected = {
        "partition", "repeats_requested", "repeats_completed", "chat_call_count", "temperature", "reasoning", "store",
        "allowed_http_routes", "model_load_requested", "model_unload_requested", "model_download_requested",
        "model_configuration_requested", "model_load_telemetry", "zero_model_load_events_when_observed",
        "sqlite_mutation", "mem0_mutation", "qdrant_mutation", "mcp_or_tools_used", "queue_persistence", "automatic_apply", "elapsed_ms",
        "repeat_executions",
    }
    if require_event_chain:
        expected.add("event_chain")
    if not isinstance(value, Mapping) or set(value) != expected:
        raise SemanticAssuranceReadinessError("readiness live execution schema is invalid")
    if value.get("partition") != "sealed_holdout" or not isinstance(value.get("repeats_requested"), int) or value["repeats_requested"] != _REQUIRED_SEALED_REPEATS or value.get("repeats_completed") != value["repeats_requested"] or not isinstance(value.get("chat_call_count"), int) or value["chat_call_count"] < 1 or not isinstance(value.get("repeat_executions"), list) or len(value["repeat_executions"]) != _REQUIRED_SEALED_REPEATS:
        raise SemanticAssuranceReadinessError("readiness live execution coverage is invalid")
    if value.get("temperature") != 0 or value.get("reasoning") != "off" or value.get("store") is not False or value.get("allowed_http_routes") != ["GET /api/v1/models", "POST /api/v1/chat"] or value.get("model_load_telemetry") != "available" or value.get("zero_model_load_events_when_observed") is not True:
        raise SemanticAssuranceReadinessError("readiness live execution contract is invalid")
    false_fields = {"model_load_requested", "model_unload_requested", "model_download_requested", "model_configuration_requested", "sqlite_mutation", "mem0_mutation", "qdrant_mutation", "mcp_or_tools_used", "queue_persistence", "automatic_apply"}
    if any(value.get(key) is not False for key in false_fields) or not isinstance(value.get("elapsed_ms"), (int, float)) or not math.isfinite(float(value["elapsed_ms"])) or float(value["elapsed_ms"]) < 0:
        raise SemanticAssuranceReadinessError("readiness live execution has an unsafe side effect")
    result = {"partition": "sealed_holdout", "repeats_requested": value["repeats_requested"], "repeats_completed": value["repeats_completed"], "chat_call_count": value["chat_call_count"], "repeat_executions": value["repeat_executions"]}
    if require_event_chain:
        result["event_chain"] = value["event_chain"]
    return result


def _live_inventory(value: object) -> None:
    expected = {"before", "after"}
    item_expected = {"exact_requested_model_loaded", "only_requested_llm_loaded", "loaded_llm_count"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise SemanticAssuranceReadinessError("readiness live inventory is invalid")
    for item in value.values():
        if not isinstance(item, Mapping) or set(item) != item_expected or item != {"exact_requested_model_loaded": True, "only_requested_llm_loaded": True, "loaded_llm_count": 1}:
            raise SemanticAssuranceReadinessError("readiness live inventory does not prove exact model isolation")


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
        if v6_label_admission.get("receipt_schema_version") == LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V4:
            expected_dataset.update({
                "label_independence": "attested_structurally_bound_with_v6_curator_audited_corpus_not_identity_authenticated",
                "label_admission_version": "v4_v6_curator_audited",
                "admission_profile": v6_label_admission["admission_profile"],
                "partition_risk_counts": v6_label_admission["partition_risk_counts"],
                "v6_control_binding_digest": v6_label_admission["v6_control_binding_digest"],
                "curator_audit": v6_label_admission["curator_audit"],
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
    except Exception as exc:
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
    return {
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


__all__ = [
    "RECEIPT_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION_V3",
    "RECEIPT_SCHEMA_VERSION_V4",
    "RECEIPT_SCHEMA_VERSION_V5",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_V3",
    "SCHEMA_VERSION_V4",
    "SCHEMA_VERSION_V5",
    "SemanticAssuranceReadinessError",
    "assess_semantic_assurance_readiness",
    "assess_semantic_assurance_readiness_v3",
    "assess_semantic_assurance_readiness_v4",
    "assess_semantic_assurance_readiness_v5",
    "load_semantic_assurance_readiness",
    "write_semantic_assurance_readiness_receipt",
]
