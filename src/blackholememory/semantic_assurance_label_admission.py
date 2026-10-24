"""Fail-closed admission of independently reviewed semantic-assurance labels.

This module verifies *structural evidence* for a local restricted corpus: two
separate reviewer label sets, one blind adjudication set, exact corpus binding,
and fail-closed split coverage.  It deliberately does not authenticate a
person, inspect source evidence, or turn an attestation into a claim of
universal semantic truth.  The returned receipt is content-free.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .semantic_assurance_label_admission_base import (
    _LABEL_KEYS,  # noqa: F401
    _MAX_MANIFEST_BYTES,
    _REQUIRED_HUMAN_ISOLATION_KEYS,
    _REQUIRED_RISK_CLASSES,  # noqa: F401
    _SEALED_CRITICAL_RISK_CLASSES,  # noqa: F401
    _V6_ADMISSION_PROFILE,  # noqa: F401
    _V6_CURATOR_AUDITED_ADMISSION_PROFILE,  # noqa: F401
    RECEIPT_SCHEMA_VERSION,
    RECEIPT_SCHEMA_VERSION_V2,
    RECEIPT_SCHEMA_VERSION_V3,
    RECEIPT_SCHEMA_VERSION_V4,
    RECEIPT_SCHEMA_VERSION_V5,
    RECEIPT_SCHEMA_VERSION_V6,
    SCHEMA_VERSION,
    SCHEMA_VERSION_V2,
    SCHEMA_VERSION_V3,
    SCHEMA_VERSION_V4,
    SCHEMA_VERSION_V5,
    SCHEMA_VERSION_V6,
    SemanticAssuranceLabelAdmissionError,
    _curator_audit_binding,  # noqa: F401
    _dataset_binding,
    _digest,
    _digest_text,
    _enum,  # noqa: F401
    _final_dataset_binding,  # noqa: F401
    _iso_timestamp,  # noqa: F401
    _partition_risk_count_map,  # noqa: F401
    _partition_risk_counts,  # noqa: F401
    _risk_count_map,  # noqa: F401
    _sealed_holdout_commitment,  # noqa: F401
    _text,
    _validate_adjudications,
    _validate_corpus,
    _validate_corpus_admission,
    _validate_label_rows,  # noqa: F401
    _validate_labels,  # noqa: F401
    _validate_pre_registration,
    _validate_protocol,
    _validate_receipt_corpus_admission,
    _validate_receipt_pre_registration,
    _validate_reviewers,
)
from .semantic_assurance_label_admission_strata import (
    _V6_SEALED_ALIGNED_CONFLICT_LABEL_MINIMUM,
    _V6_SEALED_ALIGNED_CONFLICT_LABELS,  # noqa: F401
    _V6_SEALED_EXACT_LABEL_STRATA,  # noqa: F401
    _V6_SEALED_EXACT_LABEL_STRATUM_MINIMUM,
    _V6_SEALED_RISK_LABEL_MINIMUM_RISK_CLASSES,
    V5_READINESS_STRATA_DIAGNOSTIC_SCHEMA_VERSION,
    _v6_sealed_exact_label_strata_binding,
    _v6_sealed_exact_label_strata_binding_from_counts,  # noqa: F401
    _v6_sealed_exact_label_strata_counts,  # noqa: F401
    _v6_sealed_exact_label_strata_report,  # noqa: F401
    _v6_sealed_risk_label_matrix_binding,
    _v6_sealed_risk_label_matrix_counts,  # noqa: F401
    assess_semantic_assurance_v5_readiness_strata,
)


def load_semantic_assurance_label_admission(path: Path | str, dataset: Mapping[str, Any]) -> dict[str, Any]:
    """Load and structurally admit one local human-review manifest.

    The manifest should live in a restricted, ignored evidence root.  A caller
    receives only its binding and aggregate agreement information, never case
    IDs, labels, prompts, supports, or reviewer names.
    """

    source = Path(path)
    try:
        if source.stat().st_size > _MAX_MANIFEST_BYTES:
            raise SemanticAssuranceLabelAdmissionError("human-label manifest exceeds the bounded size")
        raw = json.loads(source.read_text(encoding="utf-8"))
    except SemanticAssuranceLabelAdmissionError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticAssuranceLabelAdmissionError("human-label manifest is unreadable") from exc
    return admit_semantic_assurance_human_labels(raw, dataset)


def admit_semantic_assurance_human_labels(raw: object, dataset: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a two-reviewer, blind-adjudication assertion against a corpus.

    ``dataset`` is the normalized semantic-assurance dataset returned by its
    loader.  The function intentionally accepts no model output, transport,
    storage, or lifecycle input.
    """

    normalized_dataset = _dataset_binding(dataset)
    if dataset.get("labeling_kind") != "independent_human_review" or dataset.get("label_provenance_kind") != "independent_human_review":
        raise SemanticAssuranceLabelAdmissionError("human-label admission cannot upgrade a non-human reference fixture")
    for case in dataset["cases"]:
        isolation = case.get("isolation")
        if not isinstance(isolation, Mapping) or set(isolation) != _REQUIRED_HUMAN_ISOLATION_KEYS:
            raise SemanticAssuranceLabelAdmissionError("human-label corpus lacks required split-isolation identities")
    if not isinstance(raw, Mapping) or raw.get("schema_version") not in {SCHEMA_VERSION, SCHEMA_VERSION_V2, SCHEMA_VERSION_V3, SCHEMA_VERSION_V4, SCHEMA_VERSION_V5, SCHEMA_VERSION_V6}:
        raise SemanticAssuranceLabelAdmissionError("human-label manifest schema is unsupported")
    manifest_schema = raw["schema_version"]
    expected_fields = {"schema_version", "corpus", "protocol", "pre_registration", "reviewers", "adjudications"}
    if manifest_schema in {SCHEMA_VERSION_V2, SCHEMA_VERSION_V3, SCHEMA_VERSION_V4, SCHEMA_VERSION_V5, SCHEMA_VERSION_V6}:
        expected_fields.add("corpus_admission")
    if set(raw) != expected_fields:
        raise SemanticAssuranceLabelAdmissionError("human-label manifest has unexpected fields")
    _validate_corpus(raw.get("corpus"), normalized_dataset)
    corpus_admission = (
        _validate_corpus_admission(
            raw.get("corpus_admission"),
            dataset,
            require_v6=manifest_schema in {SCHEMA_VERSION_V3, SCHEMA_VERSION_V4, SCHEMA_VERSION_V5, SCHEMA_VERSION_V6},
            require_curator_audit=manifest_schema in {SCHEMA_VERSION_V4, SCHEMA_VERSION_V5, SCHEMA_VERSION_V6},
        )
        if manifest_schema in {SCHEMA_VERSION_V2, SCHEMA_VERSION_V3, SCHEMA_VERSION_V4, SCHEMA_VERSION_V5, SCHEMA_VERSION_V6}
        else None
    )
    _validate_protocol(raw.get("protocol"))
    pre_registration = _validate_pre_registration(raw.get("pre_registration"), dataset)
    dataset_cases = dataset["cases"]
    case_support_ids = {
        str(case["case_id"]): tuple(str(item) for item in case["support_ids"])
        for case in dataset_cases
    }
    expected_case_ids = set(case_support_ids)
    reviewers = _validate_reviewers(raw.get("reviewers"), expected_case_ids, case_support_ids)
    adjudications = _validate_adjudications(raw.get("adjudications"), expected_case_ids, case_support_ids, reviewers)
    expected_labels = {str(case["case_id"]): dict(case["expected"]) for case in dataset_cases}
    if {case_id: entry["labels"] for case_id, entry in adjudications.items()} != expected_labels:
        raise SemanticAssuranceLabelAdmissionError("adjudicated labels do not exactly bind the supplied corpus")
    label_strata = (
        _v6_sealed_exact_label_strata_binding(dataset_cases, adjudications)
        if manifest_schema in {SCHEMA_VERSION_V5, SCHEMA_VERSION_V6}
        else None
    )
    risk_label_matrix = (
        _v6_sealed_risk_label_matrix_binding(dataset_cases, adjudications)
        if manifest_schema == SCHEMA_VERSION_V6
        else None
    )

    agreement_count = sum(
        reviewers[0]["labels"][case_id] == reviewers[1]["labels"][case_id]
        for case_id in sorted(expected_case_ids)
    )
    core = {
        "schema_version": (
            RECEIPT_SCHEMA_VERSION_V6 if manifest_schema == SCHEMA_VERSION_V6
            else RECEIPT_SCHEMA_VERSION_V5 if manifest_schema == SCHEMA_VERSION_V5
            else RECEIPT_SCHEMA_VERSION_V4 if manifest_schema == SCHEMA_VERSION_V4
            else RECEIPT_SCHEMA_VERSION_V3 if manifest_schema == SCHEMA_VERSION_V3
            else RECEIPT_SCHEMA_VERSION_V2 if manifest_schema == SCHEMA_VERSION_V2
            else RECEIPT_SCHEMA_VERSION
        ),
        "ok": True,
        "corpus": (
            {**normalized_dataset, "corpus_pre_admission_digest": corpus_admission["admission_digest"]}
            if corpus_admission is not None
            else dict(normalized_dataset)
        ),
        "review": {
            "reviewer_count": 2,
            "reviewer_identity_digests": sorted(item["reviewer_id_digest"] for item in reviewers),
            "adjudicator_identity_digest": adjudications[next(iter(sorted(adjudications)))]["adjudicator_id_digest"],
            "case_count": len(expected_case_ids),
            "full_reviewer_agreement_count": agreement_count,
            "adjudicated_case_count": len(adjudications),
            "distinct_reviewer_identities": True,
            "adjudicator_distinct_from_reviewers": True,
        },
        "protocol": {
            "labels_created_outside_evaluated_model": True,
            "reviewers_blinded_to_model_outputs": True,
            "adjudicator_blinded_to_reviewer_identities": True,
            "holdout_not_used_for_tuning": True,
        },
        "pre_registration": pre_registration,
        "execution": {
            "network_used": False,
            "model_called": False,
            "sqlite_mutation": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "automatic_apply": False,
        },
        "assurance": {
            "status": "attested_structurally_bound_not_identity_authenticated",
            "human_identity_authenticated": False,
            "semantic_truth_proven": False,
            "automatic_apply_ready": False,
        },
    }
    if corpus_admission is not None:
        core["corpus_admission"] = corpus_admission
    if manifest_schema in {SCHEMA_VERSION_V3, SCHEMA_VERSION_V4, SCHEMA_VERSION_V5, SCHEMA_VERSION_V6}:
        core["assurance"] = {
            **core["assurance"],
            "v6_corpus_structurally_bound": True,
        }
    if manifest_schema == SCHEMA_VERSION_V4:
        core["assurance"] = {
            **core["assurance"],
            "v6_curator_audit_exactly_bound": True,
        }
    if manifest_schema in {SCHEMA_VERSION_V5, SCHEMA_VERSION_V6}:
        assert label_strata is not None
        core["assurance"] = {
            **core["assurance"],
            "v6_curator_audit_exactly_bound": True,
            "v6_sealed_exact_label_strata_bound": True,
            **label_strata,
        }
    if manifest_schema == SCHEMA_VERSION_V6:
        assert risk_label_matrix is not None
        core["assurance"] = {
            **core["assurance"],
            "v6_sealed_risk_label_matrix_bound": True,
            **risk_label_matrix,
        }
    return {**core, "admission_digest": _digest(core)}


def verify_semantic_assurance_label_admission(value: object) -> dict[str, Any]:
    """Revalidate a content-free admission receipt before evaluation use."""

    if not isinstance(value, Mapping):
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt is invalid")
    receipt_schema = value.get("schema_version") if isinstance(value, Mapping) else None
    expected = {"schema_version", "ok", "corpus", "review", "protocol", "pre_registration", "execution", "assurance", "admission_digest"}
    if receipt_schema in {RECEIPT_SCHEMA_VERSION_V2, RECEIPT_SCHEMA_VERSION_V3, RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5, RECEIPT_SCHEMA_VERSION_V6}:
        expected.add("corpus_admission")
    if set(value) != expected or receipt_schema not in {RECEIPT_SCHEMA_VERSION, RECEIPT_SCHEMA_VERSION_V2, RECEIPT_SCHEMA_VERSION_V3, RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5, RECEIPT_SCHEMA_VERSION_V6} or value.get("ok") is not True:
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt schema is invalid")
    corpus = value.get("corpus")
    expected_corpus_fields = {"dataset_id", "fixture_digest", "label_digest"}
    if receipt_schema in {RECEIPT_SCHEMA_VERSION_V2, RECEIPT_SCHEMA_VERSION_V3, RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5, RECEIPT_SCHEMA_VERSION_V6}:
        expected_corpus_fields.add("corpus_pre_admission_digest")
    if not isinstance(corpus, Mapping) or set(corpus) != expected_corpus_fields:
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt corpus is invalid")
    normalized_corpus = {key: _text(corpus.get(key), f"human-label receipt corpus.{key}", 160) for key in corpus}
    for key in ("fixture_digest", "label_digest"):
        _digest_text(normalized_corpus[key], f"human-label receipt corpus.{key}")
    if receipt_schema in {RECEIPT_SCHEMA_VERSION_V2, RECEIPT_SCHEMA_VERSION_V3, RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5}:
        normalized_corpus["corpus_pre_admission_digest"] = _digest_text(
            normalized_corpus["corpus_pre_admission_digest"],
            "human-label receipt corpus pre-admission digest",
        )
    review = value.get("review")
    if not isinstance(review, Mapping) or set(review) != {
        "reviewer_count", "reviewer_identity_digests", "adjudicator_identity_digest", "case_count",
        "full_reviewer_agreement_count", "adjudicated_case_count", "distinct_reviewer_identities",
        "adjudicator_distinct_from_reviewers",
    }:
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt review is invalid")
    identities = review.get("reviewer_identity_digests")
    if review.get("reviewer_count") != 2 or not isinstance(identities, list) or len(identities) != 2 or len(set(identities)) != 2:
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt reviewers are invalid")
    if any(not isinstance(item, str) for item in identities):
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt reviewer digest is invalid")
    for item in identities:
        _digest_text(item, "human-label admission receipt reviewer digest")
    adjudicator = review.get("adjudicator_identity_digest")
    if not isinstance(adjudicator, str):
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt adjudicator is invalid")
    _digest_text(adjudicator, "human-label admission receipt adjudicator")
    if adjudicator in identities or review.get("distinct_reviewer_identities") is not True or review.get("adjudicator_distinct_from_reviewers") is not True:
        raise SemanticAssuranceLabelAdmissionError("human-label admission identities are not independent")
    counts = ("case_count", "full_reviewer_agreement_count", "adjudicated_case_count")
    if any(not isinstance(review.get(key), int) or review[key] < 0 for key in counts):
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt counts are invalid")
    if review["case_count"] < 1 or review["full_reviewer_agreement_count"] > review["case_count"] or review["adjudicated_case_count"] != review["case_count"]:
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt coverage is invalid")
    expected_protocol = {
        "labels_created_outside_evaluated_model": True,
        "reviewers_blinded_to_model_outputs": True,
        "adjudicator_blinded_to_reviewer_identities": True,
        "holdout_not_used_for_tuning": True,
    }
    pre_registration = _validate_receipt_pre_registration(value.get("pre_registration"))
    expected_execution = {"network_used": False, "model_called": False, "sqlite_mutation": False, "mem0_mutation": False, "qdrant_mutation": False, "automatic_apply": False}
    expected_assurance = {"status": "attested_structurally_bound_not_identity_authenticated", "human_identity_authenticated": False, "semantic_truth_proven": False, "automatic_apply_ready": False}
    if receipt_schema in {RECEIPT_SCHEMA_VERSION_V3, RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5, RECEIPT_SCHEMA_VERSION_V6}:
        expected_assurance["v6_corpus_structurally_bound"] = True
    if receipt_schema in {RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5, RECEIPT_SCHEMA_VERSION_V6}:
        expected_assurance["v6_curator_audit_exactly_bound"] = True
    if receipt_schema in {RECEIPT_SCHEMA_VERSION_V5, RECEIPT_SCHEMA_VERSION_V6}:
        expected_assurance.update({
            "v6_sealed_exact_label_strata_bound": True,
            "sealed_readiness_strata_binding_digest": _digest_text(
                value.get("assurance", {}).get("sealed_readiness_strata_binding_digest")
                if isinstance(value.get("assurance"), Mapping) else None,
                "human-label admission receipt sealed readiness strata binding",
            ),
            "sealed_readiness_strata_minimum_population": _V6_SEALED_EXACT_LABEL_STRATUM_MINIMUM,
        })
    if receipt_schema == RECEIPT_SCHEMA_VERSION_V6:
        expected_assurance.update({
            "v6_sealed_risk_label_matrix_bound": True,
            "sealed_risk_label_matrix_binding_digest": _digest_text(
                value.get("assurance", {}).get("sealed_risk_label_matrix_binding_digest")
                if isinstance(value.get("assurance"), Mapping) else None,
                "human-label admission receipt sealed risk-label matrix binding",
            ),
            "sealed_risk_label_matrix_minimum_risk_classes": _V6_SEALED_RISK_LABEL_MINIMUM_RISK_CLASSES,
            "sealed_aligned_conflict_label_minimum_population": _V6_SEALED_ALIGNED_CONFLICT_LABEL_MINIMUM,
        })
    if value.get("protocol") != expected_protocol or value.get("execution") != expected_execution or value.get("assurance") != expected_assurance:
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt boundary is invalid")
    core = {"schema_version": receipt_schema, "ok": True, "corpus": normalized_corpus, "review": dict(review), "protocol": expected_protocol, "pre_registration": pre_registration, "execution": expected_execution, "assurance": expected_assurance}
    if receipt_schema in {RECEIPT_SCHEMA_VERSION_V2, RECEIPT_SCHEMA_VERSION_V3, RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5, RECEIPT_SCHEMA_VERSION_V6}:
        core["corpus_admission"] = _validate_receipt_corpus_admission(
            value.get("corpus_admission"),
            require_v6=receipt_schema in {RECEIPT_SCHEMA_VERSION_V3, RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5, RECEIPT_SCHEMA_VERSION_V6},
            require_curator_audit=receipt_schema in {RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5, RECEIPT_SCHEMA_VERSION_V6},
        )
        if core["corpus"]["corpus_pre_admission_digest"] != core["corpus_admission"]["admission_digest"]:
            raise SemanticAssuranceLabelAdmissionError("human-label v2 corpus pre-admission digest does not match binding")
    digest = value.get("admission_digest")
    if not isinstance(digest, str):
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt digest is invalid")
    _digest_text(digest, "human-label admission receipt digest")
    if digest != _digest(core):
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt digest mismatch")
    return {**core, "admission_digest": digest}


__all__ = [
    "RECEIPT_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION_V2",
    "RECEIPT_SCHEMA_VERSION_V3",
    "RECEIPT_SCHEMA_VERSION_V4",
    "RECEIPT_SCHEMA_VERSION_V5",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_V2",
    "SCHEMA_VERSION_V3",
    "SCHEMA_VERSION_V4",
    "SCHEMA_VERSION_V5",
    "SemanticAssuranceLabelAdmissionError",
    "V5_READINESS_STRATA_DIAGNOSTIC_SCHEMA_VERSION",
    "admit_semantic_assurance_human_labels",
    "assess_semantic_assurance_v5_readiness_strata",
    "load_semantic_assurance_label_admission",
    "verify_semantic_assurance_label_admission",
]
