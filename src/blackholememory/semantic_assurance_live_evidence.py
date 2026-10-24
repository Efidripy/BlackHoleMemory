"""Evidence projection and verification for live semantic assurance."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from blackholememory.semantic_assurance_live_base import (
    LIVE_PRIVATE_EVALUATION_BUNDLE_SCHEMA_VERSION_V6, LIVE_PUBLIC_RECEIPT_SCHEMA_VERSION_V6,
    LIVE_RECEIPT_SCHEMA_VERSION, LIVE_RECEIPT_SCHEMA_VERSION_V4, LIVE_RECEIPT_SCHEMA_VERSION_V5, LIVE_RECEIPT_SCHEMA_VERSION_V6,
    SemanticAssuranceLiveError, _mapping_keys, _sha256_digest,
)
from blackholememory.semantic_assurance_live_event import _digest, _is_sha256

def build_semantic_assurance_live_v6_evidence(
    live_receipt: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split one verified legacy live receipt into public and owner-only V6 artifacts.

    The public artifact commits to the exact legacy receipt and the exact
    offline-report sequence, but never carries those reports.  The private
    artifact then anchors itself to the completed public receipt and carries
    the reports.  Public construction therefore cannot depend on a private
    bundle digest, avoiding a digest cycle while preserving exact binding.
    """

    legacy_core, reports = _verified_legacy_live_receipt(live_receipt)
    reports_commitment = _offline_reports_commitment(reports)
    public_core = _public_live_receipt_v6_core(legacy_core, reports_commitment, len(reports))
    public_receipt = {**public_core, "public_receipt_digest": _digest(public_core)}
    bundle_core = {
        "schema_version": LIVE_PRIVATE_EVALUATION_BUNDLE_SCHEMA_VERSION_V6,
        "storage_scope": "owner_only_private_evaluation",
        "public_anchor": {
            "schema_version": LIVE_PUBLIC_RECEIPT_SCHEMA_VERSION_V6,
            "public_receipt_digest": public_receipt["public_receipt_digest"],
        },
        "legacy_live_receipt": {
            "schema_version": legacy_core["schema_version"],
            "report_digest": legacy_core["report_digest"],
        },
        "offline_reports_commitment": reports_commitment,
        "offline_reports": reports,
    }
    private_bundle = {**bundle_core, "private_bundle_digest": _digest(bundle_core)}
    _verify_public_live_receipt_v6(public_receipt)
    _verify_private_evaluation_bundle_v6(private_bundle, public_receipt=public_receipt)
    return public_receipt, private_bundle
def verify_semantic_assurance_live_v6_evidence(
    public_receipt: Mapping[str, Any],
    private_bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed unless the V6 public receipt and private reports bind exactly."""

    public_core = _verify_public_live_receipt_v6(public_receipt)
    bundle_core = _verify_private_evaluation_bundle_v6(private_bundle, public_receipt=public_receipt)
    return {
        "public_receipt_digest": _sha256_digest(
            public_receipt.get("public_receipt_digest"),
            "public receipt digest",
        ),
        "private_bundle_digest": _sha256_digest(
            private_bundle.get("private_bundle_digest"),
            "private bundle digest",
        ),
        "legacy_live_receipt_digest": public_core["legacy_live_receipt"]["report_digest"],
        "offline_reports_commitment": bundle_core["offline_reports_commitment"],
        "offline_report_count": len(bundle_core["offline_reports"]),
    }
def _verified_legacy_live_receipt(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return a digest-verified V3/V4/V5 receipt core and its private reports."""

    if not isinstance(value, Mapping):
        raise SemanticAssuranceLiveError("semantic assurance legacy live receipt is invalid")
    schema_version = value.get("schema_version")
    if schema_version not in {
        LIVE_RECEIPT_SCHEMA_VERSION,
        LIVE_RECEIPT_SCHEMA_VERSION_V4,
        LIVE_RECEIPT_SCHEMA_VERSION_V5,
        LIVE_RECEIPT_SCHEMA_VERSION_V6,
    }:
        raise SemanticAssuranceLiveError("semantic assurance legacy live receipt version is unsupported")
    report_digest = _sha256_digest(value.get("report_digest"), "legacy live receipt digest")
    core = {key: item for key, item in value.items() if key != "report_digest"}
    if _digest(core) != report_digest:
        raise SemanticAssuranceLiveError("semantic assurance legacy live receipt digest mismatch")
    reports = core.get("offline_reports")
    if not isinstance(reports, list) or any(not isinstance(report, Mapping) for report in reports):
        raise SemanticAssuranceLiveError("semantic assurance legacy offline reports are invalid")
    return {**core, "report_digest": report_digest}, [dict(report) for report in reports]
def _public_live_receipt_v6_core(
    legacy_core: Mapping[str, Any],
    reports_commitment: str,
    report_count: int,
) -> dict[str, Any]:
    """Project only bounded, content-free legacy fields into the V6 receipt."""

    dataset = legacy_core.get("dataset")
    model = legacy_core.get("model")
    prompt_contract = legacy_core.get("prompt_contract")
    execution = legacy_core.get("execution")
    gate = legacy_core.get("gate")
    event_chain = execution.get("event_chain") if isinstance(execution, Mapping) else None
    if (
        not isinstance(dataset, Mapping)
        or not isinstance(model, Mapping)
        or not isinstance(prompt_contract, Mapping)
        or not isinstance(execution, Mapping)
        or not isinstance(gate, Mapping)
        or not isinstance(event_chain, Mapping)
        or not isinstance(report_count, int)
        or report_count < 0
    ):
        raise SemanticAssuranceLiveError("semantic assurance legacy live receipt public projection is invalid")
    public_execution = {
        key: execution.get(key)
        for key in (
            "partition", "repeats_requested", "repeats_completed", "chat_call_count",
            "temperature", "reasoning", "store", "allowed_http_routes",
            "model_load_requested", "model_unload_requested", "model_download_requested",
            "model_configuration_requested", "model_load_telemetry",
            "zero_model_load_events_when_observed", "sqlite_mutation", "mem0_mutation",
            "qdrant_mutation", "mcp_or_tools_used", "queue_persistence", "automatic_apply",
            "elapsed_ms",
        )
    }
    event_chain_commitment = {
        key: event_chain.get(key)
        for key in (
            "event_count", "first_event_digest", "last_event_digest", "chain_root",
            "inventory_before_digest", "inventory_after_digest",
        )
    }
    public_gate = {
        key: gate.get(key)
        for key in (
            "mechanical_valid", "accepted", "acceptance_scope",
            "automatic_apply_ready", "automatic_apply_decision",
        )
    }
    return {
        "schema_version": LIVE_PUBLIC_RECEIPT_SCHEMA_VERSION_V6,
        "mode": "live-read-only-public-evidence",
        "legacy_live_receipt": {
            "schema_version": legacy_core["schema_version"],
            "report_digest": legacy_core["report_digest"],
        },
        "dataset": {key: dataset.get(key) for key in ("dataset_id", "fixture_digest", "label_digest")},
        "model": {key: model.get(key) for key in ("model_id", "model_variant", "endpoint")},
        "prompt_contract": {key: prompt_contract.get(key) for key in ("name", "digest")},
        "execution": {**public_execution, "event_chain_commitment": event_chain_commitment},
        "measurement": {
            "outcome": "complete" if legacy_core.get("failure") is None else "failed",
            "model_load_events": legacy_core.get("model_load_events"),
            "model_load_telemetry": legacy_core.get("model_load_telemetry"),
        },
        "gate": public_gate,
        "private_evaluation": {
            "offline_report_count": report_count,
            "offline_reports_commitment": reports_commitment,
            "label_admission_commitment": (
                _digest(legacy_core["label_admission"])
                if "label_admission" in legacy_core
                else None
            ),
        },
        "limitations": [
            "the public receipt excludes all per-item semantic content",
            "the owner-only bundle is required to inspect or recompute detailed evaluation evidence",
            "automatic semantic apply remains no_go",
        ],
    }
def _offline_reports_commitment(reports: Sequence[Mapping[str, Any]]) -> str:
    return _digest({
        "domain": "bhm.semantic-assurance-live-v6-offline-reports",
        "offline_reports": [dict(report) for report in reports],
    })
def _verify_public_live_receipt_v6(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SemanticAssuranceLiveError("semantic assurance public live receipt is invalid")
    digest = _sha256_digest(value.get("public_receipt_digest"), "public receipt digest")
    core = {key: item for key, item in value.items() if key != "public_receipt_digest"}
    expected_core_keys = {
        "schema_version", "mode", "legacy_live_receipt", "dataset", "model",
        "prompt_contract", "execution", "measurement", "gate", "private_evaluation",
        "limitations",
    }
    if (
        value.get("schema_version") != LIVE_PUBLIC_RECEIPT_SCHEMA_VERSION_V6
        or set(core) != expected_core_keys
        or core.get("mode") != "live-read-only-public-evidence"
        or _digest(core) != digest
    ):
        raise SemanticAssuranceLiveError("semantic assurance public live receipt digest mismatch")
    prohibited = {
        "offline_reports", "results", "case_id", "case_ids", "predictions",
        "support_relevance", "support_ids", "labels",
    }
    if _mapping_keys(core) & prohibited:
        raise SemanticAssuranceLiveError("semantic assurance public live receipt exposes private evaluator data")
    private = core.get("private_evaluation")
    legacy = core.get("legacy_live_receipt")
    dataset = core.get("dataset")
    model = core.get("model")
    prompt_contract = core.get("prompt_contract")
    execution = core.get("execution")
    measurement = core.get("measurement")
    gate = core.get("gate")
    expected_execution = {
        "partition", "repeats_requested", "repeats_completed", "chat_call_count",
        "temperature", "reasoning", "store", "allowed_http_routes",
        "model_load_requested", "model_unload_requested", "model_download_requested",
        "model_configuration_requested", "model_load_telemetry",
        "zero_model_load_events_when_observed", "sqlite_mutation", "mem0_mutation",
        "qdrant_mutation", "mcp_or_tools_used", "queue_persistence", "automatic_apply",
        "elapsed_ms", "event_chain_commitment",
    }
    expected_event_chain = {
        "event_count", "first_event_digest", "last_event_digest", "chain_root",
        "inventory_before_digest", "inventory_after_digest",
    }
    if (
        not isinstance(private, Mapping)
        or set(private) != {"offline_report_count", "offline_reports_commitment", "label_admission_commitment"}
        or not isinstance(private.get("offline_report_count"), int)
        or private["offline_report_count"] < 0
        or not _is_sha256(private.get("offline_reports_commitment"))
        or (private["label_admission_commitment"] is not None and not _is_sha256(private["label_admission_commitment"]))
        or not isinstance(legacy, Mapping)
        or set(legacy) != {"schema_version", "report_digest"}
        or legacy.get("schema_version") not in {LIVE_RECEIPT_SCHEMA_VERSION, LIVE_RECEIPT_SCHEMA_VERSION_V4, LIVE_RECEIPT_SCHEMA_VERSION_V5, LIVE_RECEIPT_SCHEMA_VERSION_V6}
        or not _is_sha256(legacy.get("report_digest"))
        or not isinstance(dataset, Mapping)
        or set(dataset) != {"dataset_id", "fixture_digest", "label_digest"}
        or any(not isinstance(dataset.get(key), str) or not dataset[key] for key in dataset)
        or not isinstance(model, Mapping)
        or set(model) != {"model_id", "model_variant", "endpoint"}
        or any(not isinstance(model.get(key), str) or not model[key] for key in model)
        or not isinstance(prompt_contract, Mapping)
        or set(prompt_contract) != {"name", "digest"}
        or not isinstance(prompt_contract.get("name"), str)
        or not _is_sha256(prompt_contract.get("digest"))
        or not isinstance(execution, Mapping)
        or set(execution) != expected_execution
        or not isinstance(execution.get("event_chain_commitment"), Mapping)
        or set(execution["event_chain_commitment"]) != expected_event_chain
        or any(
            not _is_sha256(execution["event_chain_commitment"].get(key))
            for key in (
                "first_event_digest", "last_event_digest", "chain_root",
                "inventory_before_digest", "inventory_after_digest",
            )
        )
        or not isinstance(execution["event_chain_commitment"].get("event_count"), int)
        or isinstance(execution["event_chain_commitment"]["event_count"], bool)
        or execution["event_chain_commitment"]["event_count"] < 0
        or not isinstance(measurement, Mapping)
        or set(measurement) != {"outcome", "model_load_events", "model_load_telemetry"}
        or measurement.get("outcome") not in {"complete", "failed"}
        or not isinstance(gate, Mapping)
        or set(gate) != {
            "mechanical_valid", "accepted", "acceptance_scope",
            "automatic_apply_ready", "automatic_apply_decision",
        }
        or not isinstance(core.get("limitations"), list)
    ):
        raise SemanticAssuranceLiveError("semantic assurance public live receipt binding is invalid")
    return core
def _verify_private_evaluation_bundle_v6(
    value: Mapping[str, Any],
    *,
    public_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SemanticAssuranceLiveError("semantic assurance private evaluation bundle is invalid")
    digest = _sha256_digest(value.get("private_bundle_digest"), "private bundle digest")
    core = {key: item for key, item in value.items() if key != "private_bundle_digest"}
    if (
        value.get("schema_version") != LIVE_PRIVATE_EVALUATION_BUNDLE_SCHEMA_VERSION_V6
        or set(core) != {
            "schema_version", "storage_scope", "public_anchor", "legacy_live_receipt",
            "offline_reports_commitment", "offline_reports",
        }
        or _digest(core) != digest
        or core.get("storage_scope") != "owner_only_private_evaluation"
    ):
        raise SemanticAssuranceLiveError("semantic assurance private evaluation bundle digest mismatch")
    reports = core.get("offline_reports")
    anchor = core.get("public_anchor")
    legacy = core.get("legacy_live_receipt")
    if (
        not isinstance(reports, list)
        or any(not isinstance(report, Mapping) for report in reports)
        or not isinstance(anchor, Mapping)
        or set(anchor) != {"schema_version", "public_receipt_digest"}
        or anchor.get("schema_version") != LIVE_PUBLIC_RECEIPT_SCHEMA_VERSION_V6
        or not _is_sha256(anchor.get("public_receipt_digest"))
        or not isinstance(legacy, Mapping)
        or set(legacy) != {"schema_version", "report_digest"}
        or legacy.get("schema_version") not in {LIVE_RECEIPT_SCHEMA_VERSION, LIVE_RECEIPT_SCHEMA_VERSION_V4, LIVE_RECEIPT_SCHEMA_VERSION_V5, LIVE_RECEIPT_SCHEMA_VERSION_V6}
        or not _is_sha256(legacy.get("report_digest"))
        or not _is_sha256(core.get("offline_reports_commitment"))
        or _offline_reports_commitment(reports) != core["offline_reports_commitment"]
    ):
        raise SemanticAssuranceLiveError("semantic assurance private evaluation bundle binding is invalid")
    if public_receipt is not None:
        public_core = _verify_public_live_receipt_v6(public_receipt)
        private = public_core["private_evaluation"]
        public_legacy = public_core["legacy_live_receipt"]
        if (
            anchor.get("public_receipt_digest") != public_receipt.get("public_receipt_digest")
            or core["offline_reports_commitment"] != private["offline_reports_commitment"]
            or len(reports) != private["offline_report_count"]
            or dict(legacy) != dict(public_legacy)
        ):
            raise SemanticAssuranceLiveError("semantic assurance public and private V6 evidence is not exactly bound")
    return {**core, "offline_reports": [dict(report) for report in reports]}
