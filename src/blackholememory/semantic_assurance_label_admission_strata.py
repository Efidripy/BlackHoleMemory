"""V5 and V6 sealed exact label strata, risk-label matrix binding, and diagnostic."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .semantic_assurance_label_admission_base import (
    _REQUIRED_HUMAN_ISOLATION_KEYS,
    _REQUIRED_RISK_CLASSES,
    SCHEMA_VERSION_V4,
    SCHEMA_VERSION_V5,
    SemanticAssuranceLabelAdmissionError,
    _dataset_binding,
    _digest,
    _final_dataset_binding,
    _validate_adjudications,
    _validate_corpus,
    _validate_corpus_admission,
    _validate_pre_registration,
    _validate_protocol,
    _validate_reviewers,
)

V5_READINESS_STRATA_DIAGNOSTIC_SCHEMA_VERSION = "bhm.semantic-assurance-v5-readiness-strata-diagnostic.v1"

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
_V6_SEALED_RISK_LABEL_MINIMUM_RISK_CLASSES = 2
_V6_SEALED_ALIGNED_CONFLICT_LABEL_MINIMUM = 50
_V6_SEALED_ALIGNED_CONFLICT_LABELS = (
    ("direct_conflict", "direct"),
    ("temporal_conflict", "temporal"),
    ("scope_conflict", "scope"),
    ("implicit_conflict", "implicit"),
)


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


def _v6_sealed_risk_label_matrix_binding(
    cases: Sequence[Mapping[str, Any]],
    adjudications: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind V6 exact labels to their sealed risk classes without disclosure."""

    counts = _v6_sealed_risk_label_matrix_counts(cases, adjudications)
    for dimension, label in _V6_SEALED_EXACT_LABEL_STRATA:
        populated = sum(
            counts[(risk, dimension, label)] > 0
            for risk in _REQUIRED_RISK_CLASSES
        )
        if populated < _V6_SEALED_RISK_LABEL_MINIMUM_RISK_CLASSES:
            raise SemanticAssuranceLabelAdmissionError("human-label sealed risk-label matrix lacks stratum diversity")
    if any(
        counts[(risk, "conflict", label)] < _V6_SEALED_ALIGNED_CONFLICT_LABEL_MINIMUM
        for risk, label in _V6_SEALED_ALIGNED_CONFLICT_LABELS
    ):
        raise SemanticAssuranceLabelAdmissionError("human-label sealed risk-label matrix lacks aligned conflict evidence")
    rows = [
        {
            "risk_class": risk,
            "dimension": dimension,
            "label": label,
            "population": counts[(risk, dimension, label)],
        }
        for risk in sorted(_REQUIRED_RISK_CLASSES)
        for dimension, label in _V6_SEALED_EXACT_LABEL_STRATA
    ]
    return {
        "sealed_risk_label_matrix_binding_digest": _digest(rows),
        "sealed_risk_label_matrix_minimum_risk_classes": _V6_SEALED_RISK_LABEL_MINIMUM_RISK_CLASSES,
        "sealed_aligned_conflict_label_minimum_population": _V6_SEALED_ALIGNED_CONFLICT_LABEL_MINIMUM,
    }


def _v6_sealed_risk_label_matrix_counts(
    cases: Sequence[Mapping[str, Any]],
    adjudications: Mapping[str, Mapping[str, Any]],
) -> dict[tuple[str, str, str], int]:
    """Count the fixed sealed risk/label matrix from exact final labels."""

    counts = {
        (risk, dimension, label): 0
        for risk in _REQUIRED_RISK_CLASSES
        for dimension, label in _V6_SEALED_EXACT_LABEL_STRATA
    }
    for case in cases:
        if case.get("partition") != "sealed_holdout":
            continue
        risk = case.get("risk_class")
        if risk not in _REQUIRED_RISK_CLASSES:
            raise SemanticAssuranceLabelAdmissionError("human-label sealed risk class is invalid")
        entry = adjudications.get(str(case.get("case_id")))
        labels = entry.get("labels") if isinstance(entry, Mapping) else None
        if not isinstance(labels, Mapping):
            raise SemanticAssuranceLabelAdmissionError("human-label sealed adjudications are invalid")
        for dimension, label in _V6_SEALED_EXACT_LABEL_STRATA:
            if dimension == "support_relevance":
                relevance = labels.get(dimension)
                if not isinstance(relevance, Mapping):
                    raise SemanticAssuranceLabelAdmissionError("human-label sealed relevance is invalid")
                counts[(risk, dimension, label)] += sum(value == label for value in relevance.values())
            elif labels.get(dimension) == label:
                counts[(risk, dimension, label)] += 1
    return counts


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

