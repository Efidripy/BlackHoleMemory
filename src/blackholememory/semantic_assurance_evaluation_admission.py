"""Human-label admission receipt and corpus risk-binding validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .semantic_assurance_evaluation_base import (
    PARTITIONS,
    SemanticAssuranceEvaluationError,
    _digest,
    _sha256_digest,
)


_LABEL_ADMISSION_RECEIPT_V1 = "bhm.semantic-assurance-human-label-admission-receipt.v1"
_LABEL_ADMISSION_RECEIPT_V2 = "bhm.semantic-assurance-human-label-admission-receipt.v2"
_LABEL_ADMISSION_RECEIPT_V3 = "bhm.semantic-assurance-human-label-admission-receipt.v3"
_LABEL_ADMISSION_RECEIPT_V4 = "bhm.semantic-assurance-human-label-admission-receipt.v4"
_LABEL_ADMISSION_RECEIPT_V5 = "bhm.semantic-assurance-human-label-admission-receipt.v5"
_LABEL_ADMISSION_RECEIPT_V6 = "bhm.semantic-assurance-human-label-admission-receipt.v6"
_V2_CORPUS_ADMISSION_KEYS = frozenset({
    "receipt_schema_version",
    "admission_digest",
    "case_count",
    "partition_counts",
    "sealed_support_count",
    "packet_payload_set_digest",
    "curator_ledger_digest",
    "curator_attestation_digest",
    "dataset_binding_digest",
    "sealed_case_binding_digest",
    "sealed_support_binding_digest",
    "risk_counts",
    "sealed_risk_counts",
})
_V6_RISK_CLASSES = frozenset({
    "factuality",
    "entailment",
    "relevance",
    "direct_conflict",
    "temporal_conflict",
    "scope_conflict",
    "implicit_conflict",
    "injection",
    "cross_project",
    "retrieval_pressure",
    "abstention",
})
_V3_CORPUS_ADMISSION_KEYS = _V2_CORPUS_ADMISSION_KEYS | frozenset({
    "admission_profile",
    "partition_risk_counts",
    "v6_control_binding_digest",
})
_V4_CURATOR_AUDIT_KEYS = frozenset({
    "receipt_schema_version",
    "audit_digest",
    "atomic_fact_binding_digest",
    "control_binding_digest",
    "canonical_fragment_binding_digest",
})
_V4_CORPUS_ADMISSION_KEYS = _V2_CORPUS_ADMISSION_KEYS | frozenset({
    "admission_profile",
    "partition_risk_counts",
    "v6_control_binding_digest",
    "curator_audit",
})


def _validate_label_admission(raw: Mapping[str, Any] | None, dataset: Mapping[str, Any]) -> dict[str, Any]:
    """Bind an optional content-free dual-review receipt to this exact corpus.

    The import stays local to avoid a module cycle: the admission contract uses
    the label enums from this module, while this evaluator only needs its final
    receipt verifier.  A missing receipt is explicit lower-tier evidence, not
    a permissive substitute.
    """

    if raw is None:
        return {
            "admitted": False,
            "status": "not_verified_by_evaluator",
            "version": "none",
            "admission_digest": "not_provided",
            "corpus_pre_admission_digest": "not_provided",
            "safety_case_eligible": False,
            "acceptance_scope": "proposal_only_fixture_measurement",
        }
    try:
        from .semantic_assurance_label_admission import SemanticAssuranceLabelAdmissionError
        from .semantic_assurance_label_admission import verify_semantic_assurance_label_admission

        receipt = verify_semantic_assurance_label_admission(raw)
    except SemanticAssuranceLabelAdmissionError as exc:
        raise SemanticAssuranceEvaluationError("semantic assurance human-label admission is invalid") from exc
    corpus = receipt["corpus"]
    expected = {
        "dataset_id": dataset["dataset_id"],
        "fixture_digest": dataset["fixture_digest"],
        "label_digest": dataset["label_digest"],
    }
    if not isinstance(corpus, Mapping) or any(corpus.get(key) != value for key, value in expected.items()):
        raise SemanticAssuranceEvaluationError("semantic assurance human-label admission does not bind this exact dataset")
    schema_version = receipt.get("schema_version")
    if schema_version == _LABEL_ADMISSION_RECEIPT_V1:
        if set(corpus) != set(expected):
            raise SemanticAssuranceEvaluationError("semantic assurance v1 human-label admission corpus is invalid")
        return {
            "admitted": True,
            "status": "attested_structurally_bound_not_identity_authenticated",
            "version": "v1_fixture_mechanics_only",
            "admission_digest": receipt["admission_digest"],
            "corpus_pre_admission_digest": "not_provided",
            "safety_case_eligible": False,
            "acceptance_scope": "proposal_only_structural_label_measurement",
        }
    if schema_version not in {
        _LABEL_ADMISSION_RECEIPT_V2,
        _LABEL_ADMISSION_RECEIPT_V3,
        _LABEL_ADMISSION_RECEIPT_V4,
        _LABEL_ADMISSION_RECEIPT_V5,
        _LABEL_ADMISSION_RECEIPT_V6,
    }:
        raise SemanticAssuranceEvaluationError("semantic assurance human-label admission receipt version is unsupported")
    is_v6 = schema_version in {_LABEL_ADMISSION_RECEIPT_V3, _LABEL_ADMISSION_RECEIPT_V4, _LABEL_ADMISSION_RECEIPT_V5, _LABEL_ADMISSION_RECEIPT_V6}
    is_v6_curator_audited = schema_version in {_LABEL_ADMISSION_RECEIPT_V4, _LABEL_ADMISSION_RECEIPT_V5, _LABEL_ADMISSION_RECEIPT_V6}
    is_v6_strata_bound = schema_version in {_LABEL_ADMISSION_RECEIPT_V5, _LABEL_ADMISSION_RECEIPT_V6}
    is_v6_risk_matrix_bound = schema_version == _LABEL_ADMISSION_RECEIPT_V6
    corpus_pre_admission_digest = _sha256_digest(
        corpus.get("corpus_pre_admission_digest"),
        "semantic assurance corpus pre-admission digest",
    )
    corpus_admission = receipt.get("corpus_admission")
    expected_corpus_admission_keys = (
        _V4_CORPUS_ADMISSION_KEYS
        if is_v6_curator_audited
        else _V3_CORPUS_ADMISSION_KEYS if is_v6 else _V2_CORPUS_ADMISSION_KEYS
    )
    if not isinstance(corpus_admission, Mapping) or set(corpus_admission) != expected_corpus_admission_keys:
        raise SemanticAssuranceEvaluationError("semantic assurance corpus pre-admission binding is invalid")
    _sha256_digest(
        corpus_admission.get("curator_attestation_digest"),
        "semantic assurance v2 curator attestation binding",
    )
    for key in ("dataset_binding_digest", "sealed_case_binding_digest", "sealed_support_binding_digest"):
        _sha256_digest(corpus_admission.get(key), f"semantic assurance v2 {key}")
    if not isinstance(corpus_admission.get("risk_counts"), Mapping) or not isinstance(corpus_admission.get("sealed_risk_counts"), Mapping):
        raise SemanticAssuranceEvaluationError("semantic assurance v2 corpus risk binding is invalid")
    rows = [
        {"case_id": str(case["case_id"]), "partition": str(case["partition"]), "risk_class": str(case["risk_class"]), "support_ids": sorted(str(item) for item in case["support_ids"])}
        for case in dataset["cases"]
    ]
    rows.sort(key=lambda row: row["case_id"])
    sealed_rows = [row for row in rows if row["partition"] == "sealed_holdout"]
    risk_counts = {risk: sum(row["risk_class"] == risk for row in rows) for risk in sorted(corpus_admission["risk_counts"])}
    sealed_risk_counts = {risk: sum(row["risk_class"] == risk for row in sealed_rows) for risk in sorted(corpus_admission["sealed_risk_counts"])}
    if corpus_admission.get("dataset_binding_digest") != _digest(rows) or corpus_admission.get("sealed_case_binding_digest") != _digest(sealed_rows) or corpus_admission.get("sealed_support_binding_digest") != _digest([{"case_id": row["case_id"], "support_ids": row["support_ids"]} for row in sealed_rows]) or corpus_admission.get("risk_counts") != risk_counts or corpus_admission.get("sealed_risk_counts") != sealed_risk_counts:
        raise SemanticAssuranceEvaluationError("semantic assurance v2 corpus dataset binding does not match this dataset")
    if _sha256_digest(
        corpus_admission.get("admission_digest"),
        "semantic assurance v2 corpus admission binding",
    ) != corpus_pre_admission_digest:
        raise SemanticAssuranceEvaluationError("semantic assurance v2 corpus pre-admission digest does not bind the receipt")
    result = {
        "admitted": True,
        "status": "attested_structurally_bound_with_corpus_pre_admission_not_identity_authenticated",
        "version": "v2_corpus_pre_admitted",
        "admission_digest": receipt["admission_digest"],
        "corpus_pre_admission_digest": corpus_pre_admission_digest,
        "safety_case_eligible": True,
        "acceptance_scope": "proposal_only_corpus_pre_admitted_human_labelled_measurement",
    }
    if not is_v6:
        return result
    if is_v6_curator_audited:
        curator_audit = _validate_v6_curator_audited_corpus_admission_binding(corpus_admission, rows)
        curator_result = {
            **result,
            "status": "attested_structurally_bound_with_v6_curator_audited_corpus_not_identity_authenticated",
            "version": "v6_v6_curator_audited_risk_matrix_bound" if is_v6_risk_matrix_bound else "v5_v6_curator_audited_strata_bound" if is_v6_strata_bound else "v4_v6_curator_audited",
            "acceptance_scope": "proposal_only_v6_curator_audited_human_labelled_measurement",
            "admission_profile": "v6_curator_audited_independent_human_curation",
            "partition_risk_counts": {
                partition: dict(sorted(corpus_admission["partition_risk_counts"][partition].items()))
                for partition in sorted(PARTITIONS)
            },
            "v6_control_binding_digest": _sha256_digest(
                corpus_admission.get("v6_control_binding_digest"),
                "semantic assurance V6 control binding",
            ),
            "curator_audit": curator_audit,
        }
        if is_v6_strata_bound:
            assurance = receipt.get("assurance")
            if (
                not isinstance(assurance, Mapping)
                or assurance.get("v6_sealed_exact_label_strata_bound") is not True
                or assurance.get("sealed_readiness_strata_minimum_population") != 80
            ):
                raise SemanticAssuranceEvaluationError("semantic assurance V5 strata binding is invalid")
            curator_result["sealed_readiness_strata_binding_digest"] = _sha256_digest(
                assurance.get("sealed_readiness_strata_binding_digest"),
                "semantic assurance V5 strata binding",
            )
            curator_result["sealed_readiness_strata_minimum_population"] = 80
        if is_v6_risk_matrix_bound:
            from .semantic_assurance_label_admission import _v6_sealed_risk_label_matrix_binding

            expected_matrix = _v6_sealed_risk_label_matrix_binding(
                dataset["cases"],
                {str(case["case_id"]): {"labels": case["expected"]} for case in dataset["cases"]},
            )
            if (
                assurance.get("v6_sealed_risk_label_matrix_bound") is not True
                or assurance.get("sealed_risk_label_matrix_binding_digest") != expected_matrix["sealed_risk_label_matrix_binding_digest"]
                or assurance.get("sealed_risk_label_matrix_minimum_risk_classes") != 2
                or assurance.get("sealed_aligned_conflict_label_minimum_population") != 50
            ):
                raise SemanticAssuranceEvaluationError("semantic assurance V6 risk-label matrix binding is invalid")
            curator_result.update(expected_matrix)
        return curator_result
    _validate_v6_corpus_admission_binding(corpus_admission, rows)
    return {
        **result,
        "status": "attested_structurally_bound_with_v6_corpus_pre_admission_not_identity_authenticated",
        "version": "v3_v6_corpus_pre_admitted",
        "acceptance_scope": "proposal_only_v6_corpus_pre_admitted_human_labelled_measurement",
        "admission_profile": "v6_independent_human_curation",
        "partition_risk_counts": {
            partition: dict(sorted(corpus_admission["partition_risk_counts"][partition].items()))
            for partition in sorted(PARTITIONS)
        },
        "v6_control_binding_digest": _sha256_digest(
            corpus_admission.get("v6_control_binding_digest"),
            "semantic assurance V6 control binding",
        ),
    }


def _validate_v6_corpus_admission_binding(
    corpus_admission: Mapping[str, Any],
    rows: Sequence[Mapping[str, str | list[str]]],
) -> None:
    """Recompute the V6 distribution instead of trusting propagated evidence."""

    if (
        corpus_admission.get("receipt_schema_version") != "bhm.semantic-assurance-corpus-admission-receipt.v2"
        or corpus_admission.get("admission_profile") != "v6_independent_human_curation"
    ):
        raise SemanticAssuranceEvaluationError("semantic assurance V6 corpus admission tier is invalid")
    _sha256_digest(corpus_admission.get("v6_control_binding_digest"), "semantic assurance V6 control binding")
    _validate_v6_partition_risk_distribution(corpus_admission, rows)


def _validate_v6_partition_risk_distribution(
    corpus_admission: Mapping[str, Any],
    rows: Sequence[Mapping[str, str | list[str]]],
) -> None:
    """Recompute the V6 partition/risk grid from this exact dataset."""

    partition_risk_counts = corpus_admission.get("partition_risk_counts")
    if not isinstance(partition_risk_counts, Mapping) or set(partition_risk_counts) != PARTITIONS:
        raise SemanticAssuranceEvaluationError("semantic assurance V6 partition risk distribution is invalid")
    expected = {
        partition: {
            risk: sum(row["partition"] == partition and row["risk_class"] == risk for row in rows)
            for risk in sorted(_V6_RISK_CLASSES)
        }
        for partition in sorted(PARTITIONS)
    }
    for partition in sorted(PARTITIONS):
        observed = partition_risk_counts.get(partition)
        if (
            not isinstance(observed, Mapping)
            or set(observed) != _V6_RISK_CLASSES
            or any(
                not isinstance(observed[risk], int)
                or isinstance(observed[risk], bool)
                or observed[risk] < 1
                for risk in _V6_RISK_CLASSES
            )
            or dict(observed) != expected[partition]
        ):
            raise SemanticAssuranceEvaluationError("semantic assurance V6 partition risk distribution does not bind this dataset")


def _validate_v6_curator_audited_corpus_admission_binding(
    corpus_admission: Mapping[str, Any],
    rows: Sequence[Mapping[str, str | list[str]]],
) -> dict[str, str]:
    """Require the forward-only curator-audited V6 corpus tier.

    This is deliberately separate from :func:`_validate_v6_corpus_admission_binding`.
    A v2 corpus receipt may be structurally V6-complete, but it cannot be
    upgraded to canonical-fragment-audited evidence by adding optional fields.
    """

    if (
        corpus_admission.get("receipt_schema_version")
        != "bhm.semantic-assurance-corpus-admission-receipt.v3"
        or corpus_admission.get("admission_profile")
        != "v6_curator_audited_independent_human_curation"
    ):
        raise SemanticAssuranceEvaluationError("semantic assurance curator-audited corpus admission tier is invalid")
    curator_audit = corpus_admission.get("curator_audit")
    if not isinstance(curator_audit, Mapping) or set(curator_audit) != _V4_CURATOR_AUDIT_KEYS:
        raise SemanticAssuranceEvaluationError("semantic assurance curator audit binding is invalid")
    if curator_audit.get("receipt_schema_version") != "bhm.semantic-assurance-curator-audit-receipt.v1":
        raise SemanticAssuranceEvaluationError("semantic assurance curator audit receipt version is invalid")
    normalized_audit = {
        key: _sha256_digest(curator_audit.get(key), f"semantic assurance curator audit {key}")
        for key in sorted(_V4_CURATOR_AUDIT_KEYS - {"receipt_schema_version"})
    }
    _validate_v6_partition_risk_distribution(corpus_admission, rows)
    return {
        "receipt_schema_version": "bhm.semantic-assurance-curator-audit-receipt.v1",
        **normalized_audit,
    }



__all__ = [
    "_LABEL_ADMISSION_RECEIPT_V1",
    "_LABEL_ADMISSION_RECEIPT_V2",
    "_LABEL_ADMISSION_RECEIPT_V3",
    "_LABEL_ADMISSION_RECEIPT_V4",
    "_LABEL_ADMISSION_RECEIPT_V5",
    "_LABEL_ADMISSION_RECEIPT_V6",
    "_V2_CORPUS_ADMISSION_KEYS",
    "_V3_CORPUS_ADMISSION_KEYS",
    "_V4_CORPUS_ADMISSION_KEYS",
    "_V4_CURATOR_AUDIT_KEYS",
    "_V6_RISK_CLASSES",
    "_validate_label_admission",
    "_validate_v6_corpus_admission_binding",
    "_validate_v6_curator_audited_corpus_admission_binding",
    "_validate_v6_partition_risk_distribution",
]
