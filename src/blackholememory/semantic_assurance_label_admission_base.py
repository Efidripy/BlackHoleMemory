"""Base data structures, schemas, and validators for human-label admission."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .semantic_assurance_evaluation import CONFLICT_LABELS
from .semantic_assurance_evaluation import ENTAILMENT_LABELS
from .semantic_assurance_evaluation import FACTUALITY_LABELS
from .semantic_assurance_evaluation import OPERATION_LABELS
from .semantic_assurance_evaluation import RELEVANCE_LABELS


# v1 remains available for structural parser compatibility.  It deliberately
# cannot assert that a corpus was admitted before reviewer distribution.
SCHEMA_VERSION = "bhm.semantic-assurance-human-label-admission.v1"
SCHEMA_VERSION_V2 = "bhm.semantic-assurance-human-label-admission.v2"
SCHEMA_VERSION_V3 = "bhm.semantic-assurance-human-label-admission.v3"
SCHEMA_VERSION_V4 = "bhm.semantic-assurance-human-label-admission.v4"
SCHEMA_VERSION_V5 = "bhm.semantic-assurance-human-label-admission.v5"
SCHEMA_VERSION_V6 = "bhm.semantic-assurance-human-label-admission.v6"
RECEIPT_SCHEMA_VERSION = "bhm.semantic-assurance-human-label-admission-receipt.v1"
RECEIPT_SCHEMA_VERSION_V2 = "bhm.semantic-assurance-human-label-admission-receipt.v2"
RECEIPT_SCHEMA_VERSION_V3 = "bhm.semantic-assurance-human-label-admission-receipt.v3"
RECEIPT_SCHEMA_VERSION_V4 = "bhm.semantic-assurance-human-label-admission-receipt.v4"
RECEIPT_SCHEMA_VERSION_V5 = "bhm.semantic-assurance-human-label-admission-receipt.v5"
RECEIPT_SCHEMA_VERSION_V6 = "bhm.semantic-assurance-human-label-admission-receipt.v6"

_V6_ADMISSION_PROFILE = "v6_independent_human_curation"
_V6_CURATOR_AUDITED_ADMISSION_PROFILE = "v6_curator_audited_independent_human_curation"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_LABEL_KEYS = frozenset({"factuality", "entailment", "conflict", "support_relevance", "operation"})
_REQUIRED_HUMAN_ISOLATION_KEYS = frozenset({"family_id", "group_id", "source_group_id", "source_theme", "prompt_template_id", "paraphrase_cluster_id"})
_REQUIRED_RISK_CLASSES = frozenset({
    "factuality", "entailment", "relevance", "direct_conflict", "temporal_conflict",
    "scope_conflict", "implicit_conflict", "injection", "cross_project",
    "retrieval_pressure", "abstention",
})
_SEALED_CRITICAL_RISK_CLASSES = frozenset({
    "direct_conflict", "temporal_conflict", "scope_conflict", "implicit_conflict",
    "injection", "cross_project", "retrieval_pressure", "abstention",
})


class SemanticAssuranceLabelAdmissionError(ValueError):
    """The independent-label assertion or its corpus binding is invalid."""


def _dataset_binding(dataset: Mapping[str, Any]) -> dict[str, str]:
    expected = {"dataset_id", "fixture_digest", "label_digest", "cases"}
    if not isinstance(dataset, Mapping) or not expected.issubset(dataset):
        raise SemanticAssuranceLabelAdmissionError("human-label dataset binding is invalid")
    result = {key: _text(dataset.get(key), f"human-label dataset {key}", 160) for key in ("dataset_id", "fixture_digest", "label_digest")}
    _digest_text(result["fixture_digest"], "human-label dataset fixture digest")
    _digest_text(result["label_digest"], "human-label dataset label digest")
    if not isinstance(dataset.get("cases"), Sequence) or not dataset["cases"]:
        raise SemanticAssuranceLabelAdmissionError("human-label dataset cases are invalid")
    for case in dataset["cases"]:
        if not isinstance(case, Mapping) or not isinstance(case.get("support_ids"), Sequence) or not case["support_ids"]:
            raise SemanticAssuranceLabelAdmissionError("human-label dataset support identities are invalid")
    return result


def _validate_corpus(raw: object, expected: Mapping[str, str]) -> None:
    if not isinstance(raw, Mapping) or set(raw) != {"dataset_id", "fixture_digest", "label_digest", "label_source"}:
        raise SemanticAssuranceLabelAdmissionError("human-label corpus binding is invalid")
    if raw.get("label_source") != "independent_human_review":
        raise SemanticAssuranceLabelAdmissionError("human-label corpus source is not independently reviewed")
    for key, value in expected.items():
        if raw.get(key) != value:
            raise SemanticAssuranceLabelAdmissionError("human-label corpus binding does not match dataset")


def _validate_corpus_admission(
    raw: object,
    dataset: Mapping[str, Any],
    *,
    require_v6: bool = False,
    require_curator_audit: bool = False,
) -> dict[str, Any]:
    """Bind a v2 label manifest to an independently verified pre-admission.

    The corpus receipt is intentionally content-free.  Its digest, packet and
    curator-ledger digests preserve the upstream stage boundary; exact case,
    partition and sealed-support cardinalities bind it to this normalized
    final-label dataset without exposing private review material.
    """

    try:
        from .semantic_assurance_corpus_admission import (
            SemanticAssuranceCorpusAdmissionError,
            verify_semantic_assurance_corpus_admission,
        )

        receipt = verify_semantic_assurance_corpus_admission(raw)
    except (ImportError, SemanticAssuranceCorpusAdmissionError) as exc:
        raise SemanticAssuranceLabelAdmissionError("human-label v2 corpus admission receipt is invalid") from exc

    corpus = receipt["corpus"]
    is_v6 = receipt["schema_version"] in {
        "bhm.semantic-assurance-corpus-admission-receipt.v2",
        "bhm.semantic-assurance-corpus-admission-receipt.v3",
    }
    if require_v6 != is_v6:
        raise SemanticAssuranceLabelAdmissionError("human-label corpus admission tier is invalid")
    is_curator_audited = receipt["schema_version"] == "bhm.semantic-assurance-corpus-admission-receipt.v3"
    if require_curator_audit != is_curator_audited:
        raise SemanticAssuranceLabelAdmissionError("human-label curator-audited corpus admission tier is invalid")
    expected_partitions = {
        partition: sum(case.get("partition") == partition for case in dataset["cases"])
        for partition in ("development", "validation", "sealed_holdout")
    }
    expected_sealed_supports = sum(
        len(case["support_ids"])
        for case in dataset["cases"]
        if case.get("partition") == "sealed_holdout"
    )
    dataset_binding = _final_dataset_binding(dataset["cases"])
    if (
        corpus["case_count"] != len(dataset["cases"])
        or corpus["partition_counts"] != expected_partitions
        or corpus["sealed_support_count"] != expected_sealed_supports
        or corpus["dataset_binding_digest"] != dataset_binding["dataset_binding_digest"]
        or corpus["sealed_case_binding_digest"] != dataset_binding["sealed_case_binding_digest"]
        or corpus["sealed_support_binding_digest"] != dataset_binding["sealed_support_binding_digest"]
        or corpus["risk_counts"] != dataset_binding["risk_counts"]
        or corpus["sealed_risk_counts"] != dataset_binding["sealed_risk_counts"]
    ):
        raise SemanticAssuranceLabelAdmissionError("human-label v2 corpus admission does not bind exact dataset binding")
    result = {
        "receipt_schema_version": receipt["schema_version"],
        "admission_digest": receipt["admission_digest"],
        "case_count": corpus["case_count"],
        "partition_counts": dict(corpus["partition_counts"]),
        "sealed_support_count": corpus["sealed_support_count"],
        "dataset_binding_digest": corpus["dataset_binding_digest"],
        "sealed_case_binding_digest": corpus["sealed_case_binding_digest"],
        "sealed_support_binding_digest": corpus["sealed_support_binding_digest"],
        "risk_counts": dict(corpus["risk_counts"]),
        "sealed_risk_counts": dict(corpus["sealed_risk_counts"]),
        "packet_payload_set_digest": corpus["packet_payload_set_digest"],
        "curator_ledger_digest": corpus["curator_ledger_digest"],
        "curator_attestation_digest": corpus["curator_attestation_digest"],
    }
    if is_v6:
        partition_risk_counts = _partition_risk_counts(dataset["cases"])
        if (
            receipt.get("admission_profile") != (
                _V6_CURATOR_AUDITED_ADMISSION_PROFILE if is_curator_audited else _V6_ADMISSION_PROFILE
            )
            or corpus.get("partition_risk_counts") != partition_risk_counts
            or not _digest_text(corpus.get("v6_control_binding_digest"), "human-label V6 control binding digest")
        ):
            raise SemanticAssuranceLabelAdmissionError("human-label V6 corpus admission does not bind exact dataset distribution")
        result.update({
            "admission_profile": receipt["admission_profile"],
            "partition_risk_counts": partition_risk_counts,
            "v6_control_binding_digest": corpus["v6_control_binding_digest"],
        })
    if is_curator_audited:
        result["curator_audit"] = _curator_audit_binding(receipt.get("curator_audit"))
    return result


def _validate_receipt_corpus_admission(
    raw: object,
    *,
    require_v6: bool = False,
    require_curator_audit: bool = False,
) -> dict[str, Any]:
    expected_fields = {
        "receipt_schema_version", "admission_digest", "case_count", "partition_counts",
        "sealed_support_count", "dataset_binding_digest", "sealed_case_binding_digest",
        "sealed_support_binding_digest", "risk_counts", "sealed_risk_counts",
        "packet_payload_set_digest", "curator_ledger_digest",
        "curator_attestation_digest",
    }
    if require_v6:
        expected_fields.update({"admission_profile", "partition_risk_counts", "v6_control_binding_digest"})
    if require_curator_audit:
        expected_fields.add("curator_audit")
    if not isinstance(raw, Mapping) or set(raw) != expected_fields:
        raise SemanticAssuranceLabelAdmissionError("human-label v2 corpus admission binding is invalid")
    expected_schema = (
        "bhm.semantic-assurance-corpus-admission-receipt.v3" if require_curator_audit
        else "bhm.semantic-assurance-corpus-admission-receipt.v2" if require_v6
        else "bhm.semantic-assurance-corpus-admission-receipt.v1"
    )
    if raw.get("receipt_schema_version") != expected_schema:
        raise SemanticAssuranceLabelAdmissionError("human-label v2 corpus admission receipt schema is invalid")
    partition_counts = raw.get("partition_counts")
    if (
        not isinstance(partition_counts, Mapping)
        or set(partition_counts) != {"development", "validation", "sealed_holdout"}
        or any(not isinstance(partition_counts[key], int) or partition_counts[key] < 1 for key in partition_counts)
    ):
        raise SemanticAssuranceLabelAdmissionError("human-label v2 corpus admission partition counts are invalid")
    case_count = raw.get("case_count")
    sealed_support_count = raw.get("sealed_support_count")
    if (
        not isinstance(case_count, int)
        or case_count != sum(partition_counts.values())
        or not isinstance(sealed_support_count, int)
        or sealed_support_count < 1
    ):
        raise SemanticAssuranceLabelAdmissionError("human-label v2 corpus admission counts are invalid")
    risk_counts = raw.get("risk_counts")
    sealed_risk_counts = raw.get("sealed_risk_counts")
    if not _risk_count_map(risk_counts) or not _risk_count_map(sealed_risk_counts):
        raise SemanticAssuranceLabelAdmissionError("human-label v2 corpus admission risk counts are invalid")
    result = {
        "receipt_schema_version": raw["receipt_schema_version"],
        "admission_digest": _digest_text(raw.get("admission_digest"), "human-label v2 corpus admission digest"),
        "case_count": case_count,
        "partition_counts": {key: partition_counts[key] for key in sorted(partition_counts)},
        "sealed_support_count": sealed_support_count,
        "dataset_binding_digest": _digest_text(raw.get("dataset_binding_digest"), "human-label v2 dataset binding digest"),
        "sealed_case_binding_digest": _digest_text(raw.get("sealed_case_binding_digest"), "human-label v2 sealed case binding digest"),
        "sealed_support_binding_digest": _digest_text(raw.get("sealed_support_binding_digest"), "human-label v2 sealed support binding digest"),
        "risk_counts": dict(sorted(risk_counts.items())),
        "sealed_risk_counts": dict(sorted(sealed_risk_counts.items())),
        "packet_payload_set_digest": _digest_text(raw.get("packet_payload_set_digest"), "human-label v2 packet payload digest"),
        "curator_ledger_digest": _digest_text(raw.get("curator_ledger_digest"), "human-label v2 curator ledger digest"),
        "curator_attestation_digest": _digest_text(
            raw.get("curator_attestation_digest"),
            "human-label v2 curator attestation digest",
        ),
    }
    if require_v6:
        partition_risk_counts = raw.get("partition_risk_counts")
        if not _partition_risk_count_map(partition_risk_counts, partition_counts):
            raise SemanticAssuranceLabelAdmissionError("human-label V6 corpus admission partition risk counts are invalid")
        expected_profile = _V6_CURATOR_AUDITED_ADMISSION_PROFILE if require_curator_audit else _V6_ADMISSION_PROFILE
        if raw.get("admission_profile") != expected_profile:
            raise SemanticAssuranceLabelAdmissionError("human-label V6 corpus admission profile is invalid")
        result.update({
            "admission_profile": expected_profile,
            "partition_risk_counts": {
                partition: dict(sorted(partition_risk_counts[partition].items()))
                for partition in sorted(partition_risk_counts)
            },
            "v6_control_binding_digest": _digest_text(raw.get("v6_control_binding_digest"), "human-label V6 control binding digest"),
        })
    if require_curator_audit:
        result["curator_audit"] = _curator_audit_binding(raw.get("curator_audit"))
    return result


def _curator_audit_binding(raw: object) -> dict[str, Any]:
    """Validate the content-free V6 canonical-source audit link.

    A later corpus-admission v3 receipt must preserve exactly these receipt
    fields.  This module deliberately neither loads private curator inputs nor
    interprets their source fragments.
    """

    expected = {
        "receipt_schema_version", "audit_digest", "atomic_fact_binding_digest",
        "control_binding_digest", "canonical_fragment_binding_digest",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise SemanticAssuranceLabelAdmissionError("human-label curator audit binding is invalid")
    if raw.get("receipt_schema_version") != "bhm.semantic-assurance-curator-audit-receipt.v1":
        raise SemanticAssuranceLabelAdmissionError("human-label curator audit receipt schema is invalid")
    return {
        key: (
            raw["receipt_schema_version"]
            if key == "receipt_schema_version"
            else _digest_text(raw.get(key), f"human-label curator audit {key.replace('_', ' ')}")
        )
        for key in sorted(expected)
    }


def _final_dataset_binding(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [
        {
            "case_id": str(case["case_id"]),
            "partition": str(case["partition"]),
            "risk_class": str(case["risk_class"]),
            "support_ids": sorted(str(item) for item in case["support_ids"]),
        }
        for case in cases
    ]
    rows.sort(key=lambda row: row["case_id"])
    sealed_rows = [row for row in rows if row["partition"] == "sealed_holdout"]
    return {
        "dataset_binding_digest": _digest(rows),
        "sealed_case_binding_digest": _digest(sealed_rows),
        "sealed_support_binding_digest": _digest([
            {"case_id": row["case_id"], "support_ids": row["support_ids"]}
            for row in sealed_rows
        ]),
        "risk_counts": {risk: sum(row["risk_class"] == risk for row in rows) for risk in sorted(_REQUIRED_RISK_CLASSES)},
        "sealed_risk_counts": {risk: sum(row["risk_class"] == risk for row in sealed_rows) for risk in sorted(_REQUIRED_RISK_CLASSES)},
    }


def _risk_count_map(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == _REQUIRED_RISK_CLASSES
        and all(isinstance(value[key], int) and not isinstance(value[key], bool) and value[key] >= 0 for key in _REQUIRED_RISK_CLASSES)
    )


def _partition_risk_counts(cases: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    return {
        partition: {
            risk: sum(case.get("partition") == partition and case.get("risk_class") == risk for case in cases)
            for risk in sorted(_REQUIRED_RISK_CLASSES)
        }
        for partition in ("development", "validation", "sealed_holdout")
    }


def _partition_risk_count_map(value: object, partition_counts: Mapping[str, object]) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"development", "validation", "sealed_holdout"}:
        return False
    for partition in value:
        if not _risk_count_map(value[partition]) or any(value[partition][risk] < 1 for risk in _REQUIRED_RISK_CLASSES):
            return False
        if not isinstance(partition_counts.get(partition), int) or sum(value[partition].values()) != partition_counts[partition]:
            return False
    return True


def _validate_protocol(raw: object) -> None:
    expected = {
        "labels_created_outside_evaluated_model": True,
        "reviewers_blinded_to_model_outputs": True,
        "adjudicator_blinded_to_reviewer_identities": True,
        "holdout_not_used_for_tuning": True,
    }
    if raw != expected:
        raise SemanticAssuranceLabelAdmissionError("human-label protocol attestations are incomplete")


def _validate_pre_registration(raw: object, dataset: Mapping[str, Any]) -> dict[str, Any]:
    expected_fields = {
        "protocol_id", "committed_at", "sealed_holdout_commitment_digest", "threshold_policy_digest",
        "tuning_scope", "validation_is_frozen", "sealed_holdout_immutable", "labels_excluded_from_model_input",
        "required_risk_classes",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected_fields:
        raise SemanticAssuranceLabelAdmissionError("human-label pre-registration is incomplete")
    committed_at = _iso_timestamp(raw.get("committed_at"), "human-label pre-registration committed_at")
    risk_classes = raw.get("required_risk_classes")
    if not isinstance(risk_classes, list) or set(risk_classes) != _REQUIRED_RISK_CLASSES or len(risk_classes) != len(_REQUIRED_RISK_CLASSES):
        raise SemanticAssuranceLabelAdmissionError("human-label pre-registration risk taxonomy is invalid")
    expected_seal = _sealed_holdout_commitment(dataset)
    if _digest_text(raw.get("sealed_holdout_commitment_digest"), "human-label sealed holdout commitment") != expected_seal:
        raise SemanticAssuranceLabelAdmissionError("human-label sealed holdout commitment does not bind exact labels")
    all_risks = {str(case.get("risk_class")) for case in dataset["cases"]}
    sealed_risks = {str(case.get("risk_class")) for case in dataset["cases"] if case.get("partition") == "sealed_holdout"}
    if not _REQUIRED_RISK_CLASSES.issubset(all_risks) or not _SEALED_CRITICAL_RISK_CLASSES.issubset(sealed_risks):
        raise SemanticAssuranceLabelAdmissionError("human-label corpus lacks required adversarial risk coverage")
    if raw.get("tuning_scope") != "development_only" or raw.get("validation_is_frozen") is not True or raw.get("sealed_holdout_immutable") is not True or raw.get("labels_excluded_from_model_input") is not True:
        raise SemanticAssuranceLabelAdmissionError("human-label pre-registration isolation policy is unsafe")
    return {
        "protocol_id": _text(raw.get("protocol_id"), "human-label pre-registration protocol id", 160),
        "committed_at": committed_at,
        "sealed_holdout_commitment_digest": expected_seal,
        "threshold_policy_digest": _digest_text(raw.get("threshold_policy_digest"), "human-label threshold policy digest"),
        "tuning_scope": "development_only",
        "validation_is_frozen": True,
        "sealed_holdout_immutable": True,
        "labels_excluded_from_model_input": True,
        "risk_taxonomy_digest": _digest(sorted(_REQUIRED_RISK_CLASSES)),
        "required_risk_class_count": len(_REQUIRED_RISK_CLASSES),
        "sealed_critical_risk_class_count": len(_SEALED_CRITICAL_RISK_CLASSES),
    }


def _validate_receipt_pre_registration(raw: object) -> dict[str, Any]:
    expected_fields = {
        "protocol_id", "committed_at", "sealed_holdout_commitment_digest", "threshold_policy_digest",
        "tuning_scope", "validation_is_frozen", "sealed_holdout_immutable", "labels_excluded_from_model_input",
        "risk_taxonomy_digest", "required_risk_class_count", "sealed_critical_risk_class_count",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected_fields:
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt pre-registration is invalid")
    result = {
        "protocol_id": _text(raw.get("protocol_id"), "human-label receipt protocol id", 160),
        "committed_at": _iso_timestamp(raw.get("committed_at"), "human-label receipt committed_at"),
        "sealed_holdout_commitment_digest": _digest_text(raw.get("sealed_holdout_commitment_digest"), "human-label receipt sealed commitment"),
        "threshold_policy_digest": _digest_text(raw.get("threshold_policy_digest"), "human-label receipt threshold policy"),
        "tuning_scope": raw.get("tuning_scope"),
        "validation_is_frozen": raw.get("validation_is_frozen"),
        "sealed_holdout_immutable": raw.get("sealed_holdout_immutable"),
        "labels_excluded_from_model_input": raw.get("labels_excluded_from_model_input"),
        "risk_taxonomy_digest": _digest_text(raw.get("risk_taxonomy_digest"), "human-label receipt risk taxonomy"),
        "required_risk_class_count": raw.get("required_risk_class_count"),
        "sealed_critical_risk_class_count": raw.get("sealed_critical_risk_class_count"),
    }
    if result["tuning_scope"] != "development_only" or any(result[key] is not True for key in ("validation_is_frozen", "sealed_holdout_immutable", "labels_excluded_from_model_input")):
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt isolation policy is invalid")
    if result["risk_taxonomy_digest"] != _digest(sorted(_REQUIRED_RISK_CLASSES)) or result["required_risk_class_count"] != len(_REQUIRED_RISK_CLASSES) or result["sealed_critical_risk_class_count"] != len(_SEALED_CRITICAL_RISK_CLASSES):
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt risk taxonomy is invalid")
    return result


def _sealed_holdout_commitment(dataset: Mapping[str, Any]) -> str:
    sealed = [
        {"case_id": case["case_id"], "isolation": case["isolation"], "risk_class": case["risk_class"], "expected": case["expected"]}
        for case in dataset["cases"]
        if case["partition"] == "sealed_holdout"
    ]
    if not sealed:
        raise SemanticAssuranceLabelAdmissionError("human-label corpus has no sealed holdout")
    return _digest(sorted(sealed, key=lambda item: str(item["case_id"])))


def _validate_reviewers(raw: object, expected_ids: set[str], supports: Mapping[str, Sequence[str]]) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) != 2:
        raise SemanticAssuranceLabelAdmissionError("human-label manifest requires exactly two reviewers")
    result = []
    for entry in raw:
        if not isinstance(entry, Mapping) or set(entry) != {"reviewer_id_digest", "labels"}:
            raise SemanticAssuranceLabelAdmissionError("human-label reviewer is invalid")
        reviewer = _digest_text(entry.get("reviewer_id_digest"), "human-label reviewer digest")
        labels = _validate_label_rows(entry.get("labels"), expected_ids, supports, "human-label reviewer labels")
        result.append({"reviewer_id_digest": reviewer, "labels": labels})
    if result[0]["reviewer_id_digest"] == result[1]["reviewer_id_digest"]:
        raise SemanticAssuranceLabelAdmissionError("human-label reviewer identities must differ")
    return result


def _validate_adjudications(raw: object, expected_ids: set[str], supports: Mapping[str, Sequence[str]], reviewers: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) != len(expected_ids):
        raise SemanticAssuranceLabelAdmissionError("human-label adjudications must cover each case once")
    reviewer_digests = {str(item["reviewer_id_digest"]) for item in reviewers}
    result: dict[str, dict[str, Any]] = {}
    adjudicator_digest: str | None = None
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"case_id", "adjudicator_id_digest", "labels"}:
            raise SemanticAssuranceLabelAdmissionError("human-label adjudication is invalid")
        case_id = _text(item.get("case_id"), "human-label adjudication case id", 120)
        if case_id not in expected_ids or case_id in result:
            raise SemanticAssuranceLabelAdmissionError("human-label adjudication case coverage is invalid")
        adjudicator = _digest_text(item.get("adjudicator_id_digest"), "human-label adjudicator digest")
        if adjudicator in reviewer_digests:
            raise SemanticAssuranceLabelAdmissionError("human-label adjudicator must differ from reviewers")
        if adjudicator_digest is None:
            adjudicator_digest = adjudicator
        elif adjudicator != adjudicator_digest:
            raise SemanticAssuranceLabelAdmissionError("human-label manifest must bind one adjudicator identity")
        labels = _validate_labels(item.get("labels"), supports[case_id], "human-label adjudication labels")
        result[case_id] = {"adjudicator_id_digest": adjudicator, "labels": labels}
    if set(result) != expected_ids:
        raise SemanticAssuranceLabelAdmissionError("human-label adjudication coverage is invalid")
    return result


def _validate_label_rows(raw: object, expected_ids: set[str], supports: Mapping[str, Sequence[str]], field: str) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) != len(expected_ids):
        raise SemanticAssuranceLabelAdmissionError(f"{field} must cover each case once")
    result = {}
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"case_id", "labels"}:
            raise SemanticAssuranceLabelAdmissionError(f"{field} row is invalid")
        case_id = _text(item.get("case_id"), f"{field} case id", 120)
        if case_id not in expected_ids or case_id in result:
            raise SemanticAssuranceLabelAdmissionError(f"{field} coverage is invalid")
        result[case_id] = _validate_labels(item.get("labels"), supports[case_id], field)
    return result


def _validate_labels(raw: object, support_ids: Sequence[str], field: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _LABEL_KEYS:
        raise SemanticAssuranceLabelAdmissionError(f"{field} are incomplete")
    relevance = raw.get("support_relevance")
    if not isinstance(relevance, Mapping) or set(relevance) != set(support_ids):
        raise SemanticAssuranceLabelAdmissionError(f"{field} relevance does not match exact supports")
    return {
        "factuality": _enum(raw.get("factuality"), FACTUALITY_LABELS, field),
        "entailment": _enum(raw.get("entailment"), ENTAILMENT_LABELS, field),
        "conflict": _enum(raw.get("conflict"), CONFLICT_LABELS, field),
        "operation": _enum(raw.get("operation"), OPERATION_LABELS, field),
        "support_relevance": {key: _enum(value, RELEVANCE_LABELS, field) for key, value in sorted(relevance.items())},
    }


def _enum(value: object, allowed: Sequence[str], field: str) -> str:
    text = _text(value, field, 120).casefold()
    if text not in allowed:
        raise SemanticAssuranceLabelAdmissionError(f"{field} label is invalid")
    return text


def _digest_text(value: object, field: str) -> str:
    text = _text(value, field, 64).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise SemanticAssuranceLabelAdmissionError(f"{field} must be a SHA-256 digest")
    return text


def _text(value: object, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise SemanticAssuranceLabelAdmissionError(f"{field} is invalid")
    return text


def _iso_timestamp(value: object, field: str) -> str:
    text = _text(value, field, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SemanticAssuranceLabelAdmissionError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SemanticAssuranceLabelAdmissionError(f"{field} must include timezone")
    return text


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

