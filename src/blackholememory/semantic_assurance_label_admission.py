"""Fail-closed admission of independently reviewed semantic-assurance labels.

This module verifies *structural evidence* for a local restricted corpus: two
separate reviewer label sets, one blind adjudication set, exact corpus binding,
and fail-closed split coverage.  It deliberately does not authenticate a
person, inspect source evidence, or turn an attestation into a claim of
universal semantic truth.  The returned receipt is content-free.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from collections.abc import Mapping, Sequence
from pathlib import Path
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
RECEIPT_SCHEMA_VERSION = "bhm.semantic-assurance-human-label-admission-receipt.v1"
RECEIPT_SCHEMA_VERSION_V2 = "bhm.semantic-assurance-human-label-admission-receipt.v2"
RECEIPT_SCHEMA_VERSION_V3 = "bhm.semantic-assurance-human-label-admission-receipt.v3"
RECEIPT_SCHEMA_VERSION_V4 = "bhm.semantic-assurance-human-label-admission-receipt.v4"
RECEIPT_SCHEMA_VERSION_V5 = "bhm.semantic-assurance-human-label-admission-receipt.v5"
V5_READINESS_STRATA_DIAGNOSTIC_SCHEMA_VERSION = "bhm.semantic-assurance-v5-readiness-strata-diagnostic.v1"
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
_V6_SEALED_EXACT_LABEL_STRATA = (
    ("factuality", "false"),
    ("factuality", "ambiguous"),
    ("factuality", "insufficient"),
    ("entailment", "contradicted"),
    ("entailment", "insufficient"),
    ("support_relevance", "irrelevant"),
    ("conflict", "direct"),
    ("conflict", "temporal"),
    ("conflict", "scope"),
    ("conflict", "implicit"),
)
_V6_SEALED_EXACT_LABEL_STRATUM_MINIMUM = 80


class SemanticAssuranceLabelAdmissionError(ValueError):
    """The independent-label assertion or its corpus binding is invalid."""


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
    if not isinstance(raw, Mapping) or raw.get("schema_version") not in {SCHEMA_VERSION, SCHEMA_VERSION_V2, SCHEMA_VERSION_V3, SCHEMA_VERSION_V4, SCHEMA_VERSION_V5}:
        raise SemanticAssuranceLabelAdmissionError("human-label manifest schema is unsupported")
    manifest_schema = raw["schema_version"]
    expected_fields = {"schema_version", "corpus", "protocol", "pre_registration", "reviewers", "adjudications"}
    if manifest_schema in {SCHEMA_VERSION_V2, SCHEMA_VERSION_V3, SCHEMA_VERSION_V4, SCHEMA_VERSION_V5}:
        expected_fields.add("corpus_admission")
    if set(raw) != expected_fields:
        raise SemanticAssuranceLabelAdmissionError("human-label manifest has unexpected fields")
    _validate_corpus(raw.get("corpus"), normalized_dataset)
    corpus_admission = (
        _validate_corpus_admission(
            raw.get("corpus_admission"),
            dataset,
            require_v6=manifest_schema in {SCHEMA_VERSION_V3, SCHEMA_VERSION_V4, SCHEMA_VERSION_V5},
            require_curator_audit=manifest_schema in {SCHEMA_VERSION_V4, SCHEMA_VERSION_V5},
        )
        if manifest_schema in {SCHEMA_VERSION_V2, SCHEMA_VERSION_V3, SCHEMA_VERSION_V4, SCHEMA_VERSION_V5}
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
        if manifest_schema == SCHEMA_VERSION_V5
        else None
    )

    agreement_count = sum(
        reviewers[0]["labels"][case_id] == reviewers[1]["labels"][case_id]
        for case_id in sorted(expected_case_ids)
    )
    core = {
        "schema_version": (
            RECEIPT_SCHEMA_VERSION_V5 if manifest_schema == SCHEMA_VERSION_V5
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
    if manifest_schema in {SCHEMA_VERSION_V3, SCHEMA_VERSION_V4, SCHEMA_VERSION_V5}:
        core["assurance"] = {
            **core["assurance"],
            "v6_corpus_structurally_bound": True,
        }
    if manifest_schema == SCHEMA_VERSION_V4:
        core["assurance"] = {
            **core["assurance"],
            "v6_curator_audit_exactly_bound": True,
        }
    if manifest_schema == SCHEMA_VERSION_V5:
        assert label_strata is not None
        core["assurance"] = {
            **core["assurance"],
            "v6_curator_audit_exactly_bound": True,
            "v6_sealed_exact_label_strata_bound": True,
            **label_strata,
        }
    return {**core, "admission_digest": _digest(core)}


def assess_semantic_assurance_v5_readiness_strata(
    raw: object,
    dataset: Mapping[str, Any],
) -> dict[str, Any]:
    """Diagnose V5 sealed-label coverage without admitting or changing a corpus.

    This is an owner-only, content-free gap report for a curator-audited V4
    or V5 manifest.  It validates the same exact final-adjudication binding
    that V5 relies on, then reports only the closed stratum vocabulary and
    aggregate populations/deficits.  It deliberately does *not* turn a V4
    corpus into V5 evidence, repair a label, call a model, or permit apply.
    """

    normalized_dataset = _dataset_binding(dataset)
    if (
        dataset.get("labeling_kind") != "independent_human_review"
        or dataset.get("label_provenance_kind") != "independent_human_review"
    ):
        raise SemanticAssuranceLabelAdmissionError(
            "V5 readiness diagnostic cannot upgrade a non-human reference fixture"
        )
    for case in dataset["cases"]:
        isolation = case.get("isolation")
        if not isinstance(isolation, Mapping) or set(isolation) != _REQUIRED_HUMAN_ISOLATION_KEYS:
            raise SemanticAssuranceLabelAdmissionError(
                "V5 readiness diagnostic corpus lacks required split-isolation identities"
            )
    if not isinstance(raw, Mapping) or raw.get("schema_version") not in {
        SCHEMA_VERSION_V4,
        SCHEMA_VERSION_V5,
    }:
        raise SemanticAssuranceLabelAdmissionError(
            "V5 readiness diagnostic requires a curator-audited V4 or V5 manifest"
        )
    if set(raw) != {
        "schema_version", "corpus", "corpus_admission", "protocol",
        "pre_registration", "reviewers", "adjudications",
    }:
        raise SemanticAssuranceLabelAdmissionError("V5 readiness diagnostic manifest has unexpected fields")

    _validate_corpus(raw.get("corpus"), normalized_dataset)
    corpus_admission = _validate_corpus_admission(
        raw.get("corpus_admission"),
        dataset,
        require_v6=True,
        require_curator_audit=True,
    )
    _validate_protocol(raw.get("protocol"))
    _validate_pre_registration(raw.get("pre_registration"), dataset)
    dataset_cases = dataset["cases"]
    case_support_ids = {
        str(case["case_id"]): tuple(str(item) for item in case["support_ids"])
        for case in dataset_cases
    }
    reviewers = _validate_reviewers(raw.get("reviewers"), set(case_support_ids), case_support_ids)
    adjudications = _validate_adjudications(
        raw.get("adjudications"), set(case_support_ids), case_support_ids, reviewers
    )
    expected_labels = {str(case["case_id"]): dict(case["expected"]) for case in dataset_cases}
    if {case_id: entry["labels"] for case_id, entry in adjudications.items()} != expected_labels:
        raise SemanticAssuranceLabelAdmissionError(
            "V5 readiness diagnostic adjudicated labels do not exactly bind the supplied corpus"
        )

    counts = _v6_sealed_exact_label_strata_counts(dataset_cases, adjudications)
    strata = _v6_sealed_exact_label_strata_report(counts)
    binding = _v6_sealed_exact_label_strata_binding_from_counts(counts)
    readiness_eligible = all(
        entry["deficit"] == 0
        for dimensions in strata.values()
        for entry in dimensions.values()
    )
    return {
        "schema_version": V5_READINESS_STRATA_DIAGNOSTIC_SCHEMA_VERSION,
        "ok": True,
        "readiness_eligible": readiness_eligible,
        "minimum_population": _V6_SEALED_EXACT_LABEL_STRATUM_MINIMUM,
        "strata": strata,
        "bindings": {
            "dataset_id": normalized_dataset["dataset_id"],
            "fixture_digest": normalized_dataset["fixture_digest"],
            "label_digest": normalized_dataset["label_digest"],
            "dataset_binding_digest": _final_dataset_binding(dataset_cases)["dataset_binding_digest"],
            "sealed_readiness_strata_binding_digest": binding[
                "sealed_readiness_strata_binding_digest"
            ],
            "manifest_binding_digest": _digest(raw),
            "corpus_admission_digest": corpus_admission["admission_digest"],
            "curator_audit_digest": corpus_admission["curator_audit"]["audit_digest"],
        },
        "assurance": {
            "status": "structural_v5_readiness_gap_diagnostic_not_admission",
            "human_identity_authenticated": False,
            "semantic_truth_proven": False,
            "automatic_apply_ready": False,
            "automatic_apply_decision": "no_go",
        },
        "execution": {
            "network_used": False,
            "model_called": False,
            "sqlite_mutation": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "automatic_apply": False,
        },
    }


def verify_semantic_assurance_label_admission(value: object) -> dict[str, Any]:
    """Revalidate a content-free admission receipt before evaluation use."""

    if not isinstance(value, Mapping):
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt is invalid")
    receipt_schema = value.get("schema_version") if isinstance(value, Mapping) else None
    expected = {"schema_version", "ok", "corpus", "review", "protocol", "pre_registration", "execution", "assurance", "admission_digest"}
    if receipt_schema in {RECEIPT_SCHEMA_VERSION_V2, RECEIPT_SCHEMA_VERSION_V3, RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5}:
        expected.add("corpus_admission")
    if set(value) != expected or receipt_schema not in {RECEIPT_SCHEMA_VERSION, RECEIPT_SCHEMA_VERSION_V2, RECEIPT_SCHEMA_VERSION_V3, RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5} or value.get("ok") is not True:
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt schema is invalid")
    corpus = value.get("corpus")
    expected_corpus_fields = {"dataset_id", "fixture_digest", "label_digest"}
    if receipt_schema in {RECEIPT_SCHEMA_VERSION_V2, RECEIPT_SCHEMA_VERSION_V3, RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5}:
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
    if receipt_schema in {RECEIPT_SCHEMA_VERSION_V3, RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5}:
        expected_assurance["v6_corpus_structurally_bound"] = True
    if receipt_schema in {RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5}:
        expected_assurance["v6_curator_audit_exactly_bound"] = True
    if receipt_schema == RECEIPT_SCHEMA_VERSION_V5:
        expected_assurance.update({
            "v6_sealed_exact_label_strata_bound": True,
            "sealed_readiness_strata_binding_digest": _digest_text(
                value.get("assurance", {}).get("sealed_readiness_strata_binding_digest")
                if isinstance(value.get("assurance"), Mapping) else None,
                "human-label admission receipt sealed readiness strata binding",
            ),
            "sealed_readiness_strata_minimum_population": _V6_SEALED_EXACT_LABEL_STRATUM_MINIMUM,
        })
    if value.get("protocol") != expected_protocol or value.get("execution") != expected_execution or value.get("assurance") != expected_assurance:
        raise SemanticAssuranceLabelAdmissionError("human-label admission receipt boundary is invalid")
    core = {"schema_version": receipt_schema, "ok": True, "corpus": normalized_corpus, "review": dict(review), "protocol": expected_protocol, "pre_registration": pre_registration, "execution": expected_execution, "assurance": expected_assurance}
    if receipt_schema in {RECEIPT_SCHEMA_VERSION_V2, RECEIPT_SCHEMA_VERSION_V3, RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5}:
        core["corpus_admission"] = _validate_receipt_corpus_admission(
            value.get("corpus_admission"),
            require_v6=receipt_schema in {RECEIPT_SCHEMA_VERSION_V3, RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5},
            require_curator_audit=receipt_schema in {RECEIPT_SCHEMA_VERSION_V4, RECEIPT_SCHEMA_VERSION_V5},
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


def _v6_sealed_exact_label_strata_binding(
    cases: Sequence[Mapping[str, Any]],
    adjudications: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind V5 to the final sealed human-label population without disclosure.

    V5 is forward-only.  V1--V4 receipts retain their historical structural
    meaning, while a V5 receipt can assert that a subsequently measured
    readiness experiment has enough exact-label observations for every
    mandatory stratum.  The receipt contains one digest and the fixed floor,
    never counts, labels, case IDs, or support IDs.
    """

    counts = _v6_sealed_exact_label_strata_counts(cases, adjudications)
    if any(count < _V6_SEALED_EXACT_LABEL_STRATUM_MINIMUM for count in counts.values()):
        raise SemanticAssuranceLabelAdmissionError("human-label sealed corpus lacks required exact-label strata")
    return _v6_sealed_exact_label_strata_binding_from_counts(counts)


def _v6_sealed_exact_label_strata_counts(
    cases: Sequence[Mapping[str, Any]],
    adjudications: Mapping[str, Mapping[str, Any]],
) -> dict[tuple[str, str], int]:
    """Count the fixed V5 strata from exact final adjudications only."""

    counts = {(dimension, label): 0 for dimension, label in _V6_SEALED_EXACT_LABEL_STRATA}
    for case in cases:
        if case.get("partition") != "sealed_holdout":
            continue
        case_id = str(case.get("case_id"))
        entry = adjudications.get(case_id)
        labels = entry.get("labels") if isinstance(entry, Mapping) else None
        if not isinstance(labels, Mapping):
            raise SemanticAssuranceLabelAdmissionError("human-label sealed adjudications are invalid")
        for dimension, label in _V6_SEALED_EXACT_LABEL_STRATA:
            if dimension == "support_relevance":
                relevance = labels.get(dimension)
                if not isinstance(relevance, Mapping):
                    raise SemanticAssuranceLabelAdmissionError("human-label sealed relevance is invalid")
                counts[(dimension, label)] += sum(value == label for value in relevance.values())
            elif labels.get(dimension) == label:
                counts[(dimension, label)] += 1
    return counts


def _v6_sealed_exact_label_strata_binding_from_counts(
    counts: Mapping[tuple[str, str], int],
) -> dict[str, Any]:
    if set(counts) != set(_V6_SEALED_EXACT_LABEL_STRATA) or any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in counts.values()
    ):
        raise SemanticAssuranceLabelAdmissionError("human-label sealed strata counts are invalid")
    binding = [
        {"dimension": dimension, "label": label, "population": counts[(dimension, label)]}
        for dimension, label in _V6_SEALED_EXACT_LABEL_STRATA
    ]
    return {
        "sealed_readiness_strata_binding_digest": _digest(binding),
        "sealed_readiness_strata_minimum_population": _V6_SEALED_EXACT_LABEL_STRATUM_MINIMUM,
    }


def _v6_sealed_exact_label_strata_report(
    counts: Mapping[tuple[str, str], int],
) -> dict[str, dict[str, dict[str, int]]]:
    """Render only aggregate closed-vocabulary gap information for owners."""

    binding = _v6_sealed_exact_label_strata_binding_from_counts(counts)
    minimum = binding["sealed_readiness_strata_minimum_population"]
    assert isinstance(minimum, int)
    return {
        dimension: {
            label: {
                "population": counts[(dimension, label)],
                "deficit": max(0, minimum - counts[(dimension, label)]),
            }
            for candidate_dimension, label in _V6_SEALED_EXACT_LABEL_STRATA
            if candidate_dimension == dimension
        }
        for dimension in dict.fromkeys(dimension for dimension, _ in _V6_SEALED_EXACT_LABEL_STRATA)
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
