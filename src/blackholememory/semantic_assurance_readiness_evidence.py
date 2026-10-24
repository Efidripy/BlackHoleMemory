"""Live classifier evidence, event chains, and curator-audited admission bindings."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .semantic_assurance_live import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_VARIANT,
    LIVE_PRIVATE_EVALUATION_BUNDLE_SCHEMA_VERSION_V6,
    LIVE_PUBLIC_RECEIPT_SCHEMA_VERSION_V6,
    LIVE_RECEIPT_SCHEMA_VERSION as LIVE_RECEIPT_SCHEMA_VERSION_V3,
    LIVE_RECEIPT_SCHEMA_VERSION_V4,
    LIVE_RECEIPT_SCHEMA_VERSION_V5,
    LIVE_RECEIPT_SCHEMA_VERSION_V6,
    PROMPT_CONTRACT,
    SemanticAssuranceLiveError,
    verify_semantic_assurance_live_v6_evidence,
)
from .semantic_assurance_readiness_metrics import (
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V5,
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V6,
    SemanticAssuranceReadinessError,
    _MINIMUM_SEALED_CASE_COUNT,
    _REQUIRED_RISK_CLASSES,
    _REQUIRED_SEALED_REPEATS,
    _V6_SEALED_CONFLICT_CLASSES,
    _digest,
    _digest_text,
    _offline_report,
    _text,
)


LIVE_RECEIPT_SCHEMA_VERSION = "bhm.semantic-assurance-live-receipt.v1"
LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V3 = "bhm.semantic-assurance-human-label-admission-receipt.v3"
LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V4 = "bhm.semantic-assurance-human-label-admission-receipt.v4"
_V6_ADMISSION_PROFILE = "v6_independent_human_curation"
_V6_CURATOR_AUDITED_ADMISSION_PROFILE = "v6_curator_audited_independent_human_curation"





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
    result = {
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
    return result


def _v6_curator_audited_label_admission_binding(
    admission: Mapping[str, Any],
    *,
    require_risk_label_matrix: bool = False,
) -> dict[str, Any]:
    """Extract the curator-audited binding, optionally requiring V6 matrix evidence."""

    expected_schema = (
        LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V6
        if require_risk_label_matrix else LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V5
    )
    if admission.get("schema_version") != expected_schema:
        raise SemanticAssuranceReadinessError("readiness label admission is not in the required curator-audited tier")
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
        or assurance.get("v6_sealed_exact_label_strata_bound") is not True
        or assurance.get("sealed_readiness_strata_minimum_population") != 80
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
    result = {
        "receipt_schema_version": expected_schema,
        "admission_digest": _digest_text(admission.get("admission_digest"), "readiness label admission digest"),
        "corpus_pre_admission_digest": corpus_admission_digest,
        "corpus_admission_digest": corpus_admission_digest,
        "admission_profile": _V6_CURATOR_AUDITED_ADMISSION_PROFILE,
        "partition_risk_counts": normalized_risk_counts,
        "v6_control_binding_digest": _digest_text(
            corpus_admission.get("v6_control_binding_digest"),
            "readiness v5 V6 control binding digest",
        ),
        "curator_audit": normalized_audit,
        "sealed_readiness_strata_binding_digest": _digest_text(
            assurance.get("sealed_readiness_strata_binding_digest"),
            "readiness v5 label strata binding",
        ),
        "sealed_readiness_strata_minimum_population": 80,
    }
    if require_risk_label_matrix:
        if (
            assurance.get("v6_sealed_risk_label_matrix_bound") is not True
            or assurance.get("sealed_risk_label_matrix_minimum_risk_classes") != 2
            or assurance.get("sealed_aligned_conflict_label_minimum_population") != 50
        ):
            raise SemanticAssuranceReadinessError("readiness V6 risk-label matrix binding is invalid")
        result.update({
            "sealed_risk_label_matrix_binding_digest": _digest_text(
                assurance.get("sealed_risk_label_matrix_binding_digest"),
                "readiness V6 risk-label matrix binding",
            ),
            "sealed_risk_label_matrix_minimum_risk_classes": 2,
            "sealed_aligned_conflict_label_minimum_population": 50,
        })
    return result


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


def _v6_private_public_live(
    public_receipt: object,
    private_bundle: object,
    admission: Mapping[str, Any],
    *,
    require_risk_label_matrix: bool = False,
) -> dict[str, Any]:
    """Recompute V6 readiness inputs from the owner-only report bundle.

    V6 intentionally does not recreate the legacy receipt from its public
    projection.  The projection commits to it, while this helper independently
    derives all semantic metrics from the exact private reports and confirms
    that both artifacts are bound to the same legacy receipt and report set.
    """

    if not isinstance(public_receipt, Mapping) or not isinstance(private_bundle, Mapping):
        raise SemanticAssuranceReadinessError("readiness v6 public/private live evidence is invalid")
    try:
        linkage = verify_semantic_assurance_live_v6_evidence(public_receipt, private_bundle)
    except SemanticAssuranceLiveError as exc:
        raise SemanticAssuranceReadinessError("readiness v6 public/private live evidence is invalid") from exc
    if (
        public_receipt.get("schema_version") != LIVE_PUBLIC_RECEIPT_SCHEMA_VERSION_V6
        or private_bundle.get("schema_version") != LIVE_PRIVATE_EVALUATION_BUNDLE_SCHEMA_VERSION_V6
    ):
        raise SemanticAssuranceReadinessError("readiness v6 public/private live evidence is invalid")
    if (
        require_risk_label_matrix
        and (
            not isinstance(public_receipt.get("legacy_live_receipt"), Mapping)
            or public_receipt["legacy_live_receipt"].get("schema_version") != LIVE_RECEIPT_SCHEMA_VERSION_V6
        )
    ):
        raise SemanticAssuranceReadinessError("readiness v7 requires a risk-matrix-bound legacy live receipt")
    dataset = public_receipt.get("dataset")
    admitted_dataset = {key: admission["corpus"].get(key) for key in ("dataset_id", "fixture_digest", "label_digest")}
    if not isinstance(dataset, Mapping) or dict(dataset) != admitted_dataset:
        raise SemanticAssuranceReadinessError("readiness v6 public receipt does not bind the admitted corpus")
    model = public_receipt.get("model")
    prompt = public_receipt.get("prompt_contract")
    expected_model = {"model_id": DEFAULT_MODEL_ID, "model_variant": DEFAULT_MODEL_VARIANT, "endpoint": DEFAULT_BASE_URL}
    if (
        not isinstance(model, Mapping)
        or dict(model) != expected_model
        or not isinstance(prompt, Mapping)
        or prompt.get("name") != PROMPT_CONTRACT
        or prompt.get("digest") != _digest(PROMPT_CONTRACT)
    ):
        raise SemanticAssuranceReadinessError("readiness v6 public model or prompt identity is invalid")
    public_private = public_receipt.get("private_evaluation")
    if (
        not isinstance(public_private, Mapping)
        or public_private.get("label_admission_commitment") != _digest(
            _v6_curator_audited_label_admission_binding(
                admission,
                require_risk_label_matrix=require_risk_label_matrix,
            )
        )
        or public_receipt.get("measurement") != {
            "outcome": "complete", "model_load_events": 0, "model_load_telemetry": "available",
        }
    ):
        raise SemanticAssuranceReadinessError("readiness v6 public receipt measurement binding is invalid")
    execution = public_receipt.get("execution")
    if not isinstance(execution, Mapping):
        raise SemanticAssuranceReadinessError("readiness v6 public execution is invalid")
    required_execution = {
        "partition": "sealed_holdout", "repeats_requested": _REQUIRED_SEALED_REPEATS,
        "repeats_completed": _REQUIRED_SEALED_REPEATS, "chat_call_count": 1200,
        "temperature": 0, "reasoning": "off", "store": False,
        "allowed_http_routes": ["GET /api/v1/models", "POST /api/v1/chat"],
        "model_load_requested": False, "model_unload_requested": False,
        "model_download_requested": False, "model_configuration_requested": False,
        "model_load_telemetry": "available", "zero_model_load_events_when_observed": True,
        "sqlite_mutation": False, "mem0_mutation": False, "qdrant_mutation": False,
        "mcp_or_tools_used": False, "queue_persistence": False, "automatic_apply": False,
    }
    if any(execution.get(key) != item for key, item in required_execution.items()):
        raise SemanticAssuranceReadinessError("readiness v6 public execution contract is invalid")
    elapsed = execution.get("elapsed_ms")
    event_chain = execution.get("event_chain_commitment")
    if (
        not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or not math.isfinite(float(elapsed)) or elapsed < 0
        or not isinstance(event_chain, Mapping)
        or set(event_chain) != {"event_count", "first_event_digest", "last_event_digest", "chain_root", "inventory_before_digest", "inventory_after_digest"}
        or event_chain.get("event_count") != 1200
    ):
        raise SemanticAssuranceReadinessError("readiness v6 public event-chain commitment is invalid")
    for key in ("first_event_digest", "last_event_digest", "chain_root", "inventory_before_digest", "inventory_after_digest"):
        _digest_text(event_chain.get(key), f"readiness v6 {key}")
    reports = private_bundle.get("offline_reports")
    if not isinstance(reports, list) or len(reports) != _REQUIRED_SEALED_REPEATS:
        raise SemanticAssuranceReadinessError("readiness v6 private report coverage is invalid")
    binding = _v6_curator_audited_label_admission_binding(
        admission,
        require_risk_label_matrix=require_risk_label_matrix,
    )
    summaries = [
        _offline_report(
            report, admission, dataset, expected_model, str(prompt["digest"]),
            v6_label_admission=binding,
        )
        for report in reports
    ]
    if len({summary["prediction_digest"] for summary in summaries}) != 1 or len({summary["results_digest"] for summary in summaries}) != 1:
        raise SemanticAssuranceReadinessError("readiness v6 private reports lack exact deterministic agreement")
    if require_risk_label_matrix and any(
        summary.get("risk_label_matrix_binding") != {
            "sealed_risk_label_matrix_binding_digest": binding["sealed_risk_label_matrix_binding_digest"],
            "sealed_risk_label_matrix_minimum_risk_classes": 2,
            "sealed_aligned_conflict_label_minimum_population": 50,
        }
        for summary in summaries
    ):
        raise SemanticAssuranceReadinessError("readiness v7 private reports do not bind the sealed risk-label matrix")
    expected_case_binding = admission["corpus_admission"].get("sealed_case_binding_digest")
    for repeat, (summary, report) in enumerate(zip(summaries, reports, strict=True), start=1):
        invocation = report.get("invocation") if isinstance(report, Mapping) else None
        if (
            summary["counts"]["case_count"] != _MINIMUM_SEALED_CASE_COUNT
            or summary["case_id_set_digest"] != expected_case_binding
            or not isinstance(invocation, Mapping)
            or invocation.get("repeat_number") != str(repeat)
            or invocation.get("case_count") != str(_MINIMUM_SEALED_CASE_COUNT)
            or invocation.get("case_id_set_digest") != expected_case_binding
        ):
            raise SemanticAssuranceReadinessError("readiness v6 private report coverage does not bind sealed corpus")
    summary = summaries[0]
    if execution["chat_call_count"] != sum(item["counts"]["case_count"] for item in summaries):
        raise SemanticAssuranceReadinessError("readiness v6 public chat-call coverage is invalid")
    gate = public_receipt.get("gate")
    if (
        not isinstance(gate, Mapping)
        or gate.get("mechanical_valid") is not True
        or gate.get("accepted") is not True
        or gate.get("automatic_apply_ready") is not False
        or gate.get("automatic_apply_decision") != "no_go"
    ):
        raise SemanticAssuranceReadinessError("readiness v6 public gate is invalid")
    evaluated_run_digest = _digest({
        "live_report_digest": linkage["legacy_live_receipt_digest"],
        "report_digests": [item["report_digest"] for item in summaries],
        "prediction_digest": summary["prediction_digest"],
        "results_digest": summary["results_digest"],
        "admission_digest": admission["admission_digest"],
    })
    result = {
        "public_receipt_digest": linkage["public_receipt_digest"],
        "private_bundle_digest": linkage["private_bundle_digest"],
        "legacy_live_receipt_digest": linkage["legacy_live_receipt_digest"],
        "offline_reports_commitment": linkage["offline_reports_commitment"],
        "evaluated_run_digest": evaluated_run_digest,
        "model_run_identity_digest": _digest({
            "model": expected_model, "prompt_contract": dict(prompt),
            "partition": "sealed_holdout", "repeats": _REQUIRED_SEALED_REPEATS,
            "chat_call_count": execution["chat_call_count"],
        }),
        "dataset_binding_digest": _digest(dict(dataset)),
        "prompt_contract_digest": str(prompt["digest"]),
        "counts": summary["counts"],
        "metrics": summary["metrics"],
        "stratified_metrics": summary["stratified_metrics"],
        "repeat": {"requested": _REQUIRED_SEALED_REPEATS, "completed": _REQUIRED_SEALED_REPEATS},
        "event_chain": {key: event_chain[key] for key in event_chain},
    }
    if "risk_stratified_metrics" in summary:
        result["risk_stratified_metrics"] = summary["risk_stratified_metrics"]
    return result
