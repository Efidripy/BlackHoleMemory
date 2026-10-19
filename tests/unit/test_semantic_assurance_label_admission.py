from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from blackholememory.semantic_assurance_evaluation import evaluate_semantic_assurance
from blackholememory.semantic_assurance_evaluation import load_semantic_assurance_dataset
from blackholememory.semantic_assurance_corpus_admission import verify_semantic_assurance_corpus_admission
from blackholememory.semantic_assurance_label_admission import SemanticAssuranceLabelAdmissionError
from blackholememory.semantic_assurance_label_admission import admit_semantic_assurance_human_labels
from blackholememory.semantic_assurance_label_admission import assess_semantic_assurance_v5_readiness_strata
from blackholememory.semantic_assurance_label_admission import verify_semantic_assurance_label_admission


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "semantic-assurance-v1.json"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _human_dataset(tmp_path: Path) -> dict[str, object]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["labeling"] = {"kind": "independent_human_review"}
    raw["label_provenance"] = {"kind": "independent_human_review"}
    all_risks = [
        "factuality", "entailment", "relevance", "direct_conflict", "temporal_conflict",
        "scope_conflict", "implicit_conflict", "injection", "cross_project",
        "retrieval_pressure", "abstention",
    ]
    sealed_risks = [
        "direct_conflict", "temporal_conflict", "scope_conflict", "implicit_conflict",
        "injection", "cross_project", "retrieval_pressure", "abstention",
    ]
    sealed_index = 0
    for index, case in enumerate(raw["cases"]):
        partition = case["partition"]
        case["source_group_id"] = f"source-{partition}-{case['group_id']}"
        case["source_theme"] = f"theme-{partition}-{case['family_id']}"
        case["prompt_template_id"] = f"template-{partition}-{case['case_id']}"
        case["paraphrase_cluster_id"] = f"paraphrase-{partition}-{case['case_id']}"
        if partition == "sealed_holdout":
            case["risk_class"] = sealed_risks[sealed_index]
            sealed_index += 1
        else:
            case["risk_class"] = all_risks[index % len(all_risks)]
    path = tmp_path / "independent-human-labels.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_semantic_assurance_dataset(path)


def _manifest(dataset: dict[str, object]) -> dict[str, object]:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    label_rows = [{"case_id": case["case_id"], "labels": case["expected"]} for case in cases]
    return {
        "schema_version": "bhm.semantic-assurance-human-label-admission.v1",
        "corpus": {
            "dataset_id": dataset["dataset_id"],
            "fixture_digest": dataset["fixture_digest"],
            "label_digest": dataset["label_digest"],
            "label_source": "independent_human_review",
        },
        "protocol": {
            "labels_created_outside_evaluated_model": True,
            "reviewers_blinded_to_model_outputs": True,
            "adjudicator_blinded_to_reviewer_identities": True,
            "holdout_not_used_for_tuning": True,
        },
        "pre_registration": {
            "protocol_id": "unit-protocol-v1",
            "committed_at": "2026-10-18T00:00:00Z",
            "sealed_holdout_commitment_digest": _sealed_digest(dataset),
            "threshold_policy_digest": _digest("unit-threshold-policy"),
            "tuning_scope": "development_only",
            "validation_is_frozen": True,
            "sealed_holdout_immutable": True,
            "labels_excluded_from_model_input": True,
            "required_risk_classes": [
                "factuality", "entailment", "relevance", "direct_conflict", "temporal_conflict",
                "scope_conflict", "implicit_conflict", "injection", "cross_project",
                "retrieval_pressure", "abstention",
            ],
        },
        "reviewers": [
            {"reviewer_id_digest": _digest("reviewer-a"), "labels": label_rows},
            {"reviewer_id_digest": _digest("reviewer-b"), "labels": label_rows},
        ],
        "adjudications": [
            {"case_id": row["case_id"], "adjudicator_id_digest": _digest("adjudicator"), "labels": row["labels"]}
            for row in label_rows
        ],
    }


def _sealed_digest(dataset: dict[str, object]) -> str:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    sealed = [
        {"case_id": case["case_id"], "isolation": case["isolation"], "risk_class": case["risk_class"], "expected": case["expected"]}
        for case in cases
        if case["partition"] == "sealed_holdout"
    ]
    return hashlib.sha256(json.dumps(sorted(sealed, key=lambda item: str(item["case_id"])), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _holdout_predictions(dataset: dict[str, object]) -> list[dict[str, object]]:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    return [
        {"case_id": case["case_id"], **case["expected"]}
        for case in cases
        if case["partition"] == "sealed_holdout"
    ]


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _v2_dataset(*, v6: bool = False) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    all_risks = [
        "factuality", "entailment", "relevance", "direct_conflict", "temporal_conflict",
        "scope_conflict", "implicit_conflict", "injection", "cross_project",
        "retrieval_pressure", "abstention",
    ]
    sealed_risks = [risk for risk in (
        "direct_conflict", "temporal_conflict", "scope_conflict", "implicit_conflict",
        "injection", "cross_project", "retrieval_pressure", "abstention",
    ) for _ in range(50)]
    if v6:
        sealed_risks = [risk for risk in (
            "direct_conflict", "temporal_conflict", "scope_conflict", "implicit_conflict",
        ) for _ in range(50)] + [all_risks[index % len(all_risks)] for index in range(200)]
    sequence = 0
    for partition, count in (("development", 220 if v6 else 80), ("validation", 220 if v6 else 80), ("sealed_holdout", 400)):
        for offset in range(count):
            case_id = f"opaque-{sequence:04d}"
            support_ids = (f"support-{sequence:04d}-a", f"support-{sequence:04d}-b") if partition == "sealed_holdout" else (f"support-{sequence:04d}",)
            expected = {
                "factuality": "true",
                "entailment": "supported",
                "conflict": "none",
                "operation": "no_op",
                "support_relevance": {support_id: "relevant" for support_id in support_ids},
            }
            if v6 and partition == "sealed_holdout":
                if offset < 80:
                    expected.update({"factuality": "false", "entailment": "contradicted", "conflict": "direct"})
                    expected["support_relevance"] = {
                        support_ids[0]: "irrelevant",
                        support_ids[1]: "relevant",
                    }
                elif offset < 160:
                    expected.update({"factuality": "ambiguous", "entailment": "insufficient", "conflict": "temporal"})
                elif offset < 240:
                    expected.update({"factuality": "insufficient", "conflict": "scope"})
                elif offset < 320:
                    expected["conflict"] = "implicit"
            cases.append({
                "case_id": case_id,
                "partition": partition,
                "support_ids": support_ids,
                "risk_class": sealed_risks[offset] if partition == "sealed_holdout" else all_risks[offset % len(all_risks)],
                "expected": expected,
                "isolation": {
                    "family_id": f"family-{sequence}",
                    "group_id": f"group-{sequence}",
                    "source_group_id": f"source-group-{sequence}",
                    "source_theme": f"source-theme-{sequence}",
                    "prompt_template_id": f"template-{sequence}",
                    "paraphrase_cluster_id": f"paraphrase-{sequence}",
                },
            })
            sequence += 1
    return {
        "_semantic_assurance_normalized": True,
        "dataset_id": "private-v2-structural-test",
        "fixture_digest": _digest("private-v2-fixture"),
        "label_digest": _digest("private-v2-labels"),
        "labeling_kind": "independent_human_review",
        "label_provenance_kind": "independent_human_review",
        "cases": cases,
    }


def _v6_risk_matrix_dataset() -> dict[str, object]:
    """Synthetic structural fixture for the forward-only V6 matrix contract."""

    dataset = _v2_dataset(v6=True)
    cases = dataset["cases"]
    assert isinstance(cases, list)
    sealed = [case for case in cases if case["partition"] == "sealed_holdout"]
    assert len(sealed) == 400
    conflict_by_range = (
        (0, 50, "direct"), (50, 100, "temporal"),
        (100, 150, "scope"), (150, 200, "implicit"),
        (200, 230, "direct"), (230, 260, "temporal"),
        (260, 290, "scope"), (290, 320, "implicit"),
    )
    for start, end, conflict in conflict_by_range:
        for case in sealed[start:end]:
            expected = case["expected"]
            assert isinstance(expected, dict)
            expected["conflict"] = conflict
    dataset["quality_gate"] = {
        "unsafe_non_no_op_budget": 0,
        "conflict_recall_required": 1.0,
        "unsupported_or_ambiguous_operation": "no_op",
        "automatic_apply_decision": "no_go",
    }
    return dataset


def _corpus_admission_receipt(dataset: dict[str, object], *, v6: bool = False) -> dict[str, object]:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    partition_counts = {
        partition: sum(case["partition"] == partition for case in cases)
        for partition in ("development", "sealed_holdout", "validation")
    }
    sealed_support_count = sum(
        len(case["support_ids"])
        for case in cases
        if case["partition"] == "sealed_holdout"
    )
    rows = sorted([
        {"case_id": case["case_id"], "partition": case["partition"], "risk_class": case["risk_class"], "support_ids": sorted(case["support_ids"])}
        for case in cases
    ], key=lambda item: str(item["case_id"]))
    sealed_rows = [item for item in rows if item["partition"] == "sealed_holdout"]
    risks = [
        "factuality", "entailment", "relevance", "direct_conflict", "temporal_conflict",
        "scope_conflict", "implicit_conflict", "injection", "cross_project",
        "retrieval_pressure", "abstention",
    ]
    corpus = {
        "case_count": len(cases),
        "partition_counts": partition_counts,
        "sealed_support_count": sealed_support_count,
        "dataset_binding_digest": _canonical_digest(rows),
        "sealed_case_binding_digest": _canonical_digest(sealed_rows),
        "sealed_support_binding_digest": _canonical_digest([{"case_id": item["case_id"], "support_ids": item["support_ids"]} for item in sealed_rows]),
        "risk_counts": {risk: sum(item["risk_class"] == risk for item in rows) for risk in risks},
        "sealed_risk_counts": {risk: sum(item["risk_class"] == risk for item in sealed_rows) for risk in risks},
        "packet_payload_set_digest": _digest("private-v2-packet-payload-set"),
        "curator_ledger_digest": _digest("private-v2-curator-ledger"),
        "curator_attestation_digest": _digest("private-v2-curator-attestation"),
    }
    if v6:
        corpus["partition_risk_counts"] = {
            partition: {risk: sum(item["partition"] == partition and item["risk_class"] == risk for item in rows) for risk in risks}
            for partition in ("development", "validation", "sealed_holdout")
        }
        corpus["v6_control_binding_digest"] = _digest("private-v6-control-binding")
    assurance = {
        "status": "structurally_admitted_for_human_review_not_semantic_independence_proven",
        "curator_claimed_attestation": True,
        "curator_attestation_bound_to_corpus": True,
        "curator_identity_externally_authenticated": False,
        "semantic_independence_proven": False,
        "semantic_truth_proven": False,
        "automatic_apply_ready": False,
        "automatic_apply_decision": "no_go",
    }
    if v6:
        assurance.update({
            "status": "v6_structurally_admitted_for_human_review_not_semantic_independence_proven",
            "v6_partition_distribution_bound": True,
            "atomic_fact_case_one_to_one_declared": True,
        })
    core = {
        "schema_version": "bhm.semantic-assurance-corpus-admission-receipt.v2" if v6 else "bhm.semantic-assurance-corpus-admission-receipt.v1",
        "ok": True,
        "corpus": corpus,
        "review": {
            "reviewer_packet_count": 2,
            "packet_case_count": len(cases),
            "same_payload_digest_set": True,
            "same_packet_positions": 0,
            "blank_ledger_coverage": True,
        },
        "assurance": assurance,
        "execution": {
            "network_used": False,
            "model_called": False,
            "sqlite_mutation": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "automatic_apply": False,
        },
    }
    if v6:
        core["admission_profile"] = "v6_independent_human_curation"
    receipt = {**core, "admission_digest": _canonical_digest(core)}
    assert verify_semantic_assurance_corpus_admission(receipt) == receipt
    return receipt


def _v2_manifest(dataset: dict[str, object]) -> dict[str, object]:
    manifest = _manifest(dataset)
    manifest["schema_version"] = "bhm.semantic-assurance-human-label-admission.v2"
    manifest["corpus_admission"] = _corpus_admission_receipt(dataset)
    return manifest


def _v3_manifest(dataset: dict[str, object]) -> dict[str, object]:
    manifest = _manifest(dataset)
    manifest["schema_version"] = "bhm.semantic-assurance-human-label-admission.v3"
    manifest["corpus_admission"] = _corpus_admission_receipt(dataset, v6=True)
    return manifest


def _curator_audit_binding(dataset: dict[str, object]) -> dict[str, object]:
    """Minimal content-free corpus.v3 binding for label-admission coverage."""

    admission = _corpus_admission_receipt(dataset, v6=True)
    binding = {
        "receipt_schema_version": "bhm.semantic-assurance-curator-audit-receipt.v1",
        "audit_digest": _digest("private-v6-curator-audit"),
        "atomic_fact_binding_digest": _digest("private-v6-atomic-facts"),
        "control_binding_digest": _digest("private-v6-controls"),
        "canonical_fragment_binding_digest": _digest("private-v6-fragments"),
    }
    core = {
        **admission,
        "schema_version": "bhm.semantic-assurance-corpus-admission-receipt.v3",
        "admission_profile": "v6_curator_audited_independent_human_curation",
        "curator_audit": binding,
        "assurance": {
            "status": "v6_curator_audited_structurally_admitted_for_human_review_not_semantic_independence_proven",
            "curator_claimed_attestation": True,
            "curator_attestation_bound_to_corpus": True,
            "curator_identity_externally_authenticated": False,
            "semantic_independence_proven": False,
            "semantic_truth_proven": False,
            "automatic_apply_ready": False,
            "automatic_apply_decision": "no_go",
            "v6_partition_distribution_bound": True,
            "atomic_fact_case_one_to_one_declared": True,
            "curator_audit_exactly_joined": True,
        },
    }
    core["admission_digest"] = _canonical_digest({key: value for key, value in core.items() if key != "admission_digest"})
    return core


def _v4_manifest(dataset: dict[str, object]) -> dict[str, object]:
    manifest = _manifest(dataset)
    manifest["schema_version"] = "bhm.semantic-assurance-human-label-admission.v4"
    manifest["corpus_admission"] = _curator_audit_binding(dataset)
    return manifest


def _v5_manifest(dataset: dict[str, object]) -> dict[str, object]:
    manifest = _manifest(dataset)
    manifest["schema_version"] = "bhm.semantic-assurance-human-label-admission.v5"
    manifest["corpus_admission"] = _curator_audit_binding(dataset)
    return manifest


def _v6_manifest(dataset: dict[str, object]) -> dict[str, object]:
    manifest = _v5_manifest(dataset)
    manifest["schema_version"] = "bhm.semantic-assurance-human-label-admission.v6"
    return manifest


def test_dual_review_admission_is_content_free_and_binds_exact_dataset(tmp_path: Path) -> None:
    dataset = _human_dataset(tmp_path)
    receipt = admit_semantic_assurance_human_labels(_manifest(dataset), dataset)

    assert verify_semantic_assurance_label_admission(receipt) == receipt
    assert receipt["review"]["case_count"] == len(dataset["cases"])
    assert receipt["assurance"] == {
        "status": "attested_structurally_bound_not_identity_authenticated",
        "human_identity_authenticated": False,
        "semantic_truth_proven": False,
        "automatic_apply_ready": False,
    }
    rendered = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert "sa-hold" not in rendered
    assert "support" not in rendered


def test_admission_rejects_duplicate_reviewer_or_tampered_final_label(tmp_path: Path) -> None:
    dataset = _human_dataset(tmp_path)
    duplicate = _manifest(dataset)
    reviewers = duplicate["reviewers"]
    assert isinstance(reviewers, list)
    reviewers[1]["reviewer_id_digest"] = reviewers[0]["reviewer_id_digest"]
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="identities must differ"):
        admit_semantic_assurance_human_labels(duplicate, dataset)

    tampered = _manifest(dataset)
    adjudications = tampered["adjudications"]
    assert isinstance(adjudications, list)
    adjudications[0]["labels"] = {**adjudications[0]["labels"], "operation": "no_op"}
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="do not exactly bind"):
        admit_semantic_assurance_human_labels(tampered, dataset)


def test_evaluator_marks_only_admitted_labels_safety_case_eligible(tmp_path: Path) -> None:
    dataset = _human_dataset(tmp_path)
    admission = admit_semantic_assurance_human_labels(_manifest(dataset), dataset)

    baseline = evaluate_semantic_assurance(dataset, _holdout_predictions(dataset))
    admitted = evaluate_semantic_assurance(dataset, _holdout_predictions(dataset), label_admission=admission)

    assert baseline["gate"]["mechanical_valid"] is True
    assert baseline["gate"]["safety_case_eligible"] is False
    assert admitted["gate"]["mechanical_valid"] is True
    assert admitted["gate"]["safety_case_eligible"] is False
    assert admitted["gate"]["automatic_apply_ready"] is False
    assert admitted["dataset"]["label_independence"] == "attested_structurally_bound_not_identity_authenticated"


def test_evaluator_rejects_admission_bound_to_another_dataset(tmp_path: Path) -> None:
    dataset = _human_dataset(tmp_path)
    admission = admit_semantic_assurance_human_labels(_manifest(dataset), dataset)
    tampered = {**admission, "corpus": {**admission["corpus"], "dataset_id": "other"}}

    with pytest.raises(Exception, match="human-label admission"):
        evaluate_semantic_assurance(dataset, _holdout_predictions(dataset), label_admission=tampered)


def test_admission_cannot_upgrade_the_public_synthetic_reference_fixture() -> None:
    dataset = load_semantic_assurance_dataset(FIXTURE)
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="cannot upgrade"):
        admit_semantic_assurance_human_labels(_manifest(dataset), dataset)


def test_admission_requires_exact_sealed_commitment_and_adversarial_coverage(tmp_path: Path) -> None:
    dataset = _human_dataset(tmp_path)
    wrong_commitment = _manifest(dataset)
    wrong_commitment["pre_registration"]["sealed_holdout_commitment_digest"] = "0" * 64
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="sealed holdout commitment"):
        admit_semantic_assurance_human_labels(wrong_commitment, dataset)

    missing_critical = _manifest(dataset)
    cases = dataset["cases"]
    assert isinstance(cases, list)
    next(case for case in cases if case["partition"] == "sealed_holdout")["risk_class"] = "factuality"
    missing_critical["pre_registration"]["sealed_holdout_commitment_digest"] = _sealed_digest(dataset)
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="adversarial risk coverage"):
        admit_semantic_assurance_human_labels(missing_critical, dataset)


def test_receipt_rejects_tampered_pre_registration(tmp_path: Path) -> None:
    dataset = _human_dataset(tmp_path)
    receipt = admit_semantic_assurance_human_labels(_manifest(dataset), dataset)
    tampered = {**receipt, "pre_registration": {**receipt["pre_registration"], "tuning_scope": "validation_allowed"}}
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="isolation policy"):
        verify_semantic_assurance_label_admission(tampered)


def test_v2_admission_binds_a_verified_content_free_corpus_pre_admission() -> None:
    dataset = _v2_dataset()
    receipt = admit_semantic_assurance_human_labels(_v2_manifest(dataset), dataset)

    assert receipt["schema_version"] == "bhm.semantic-assurance-human-label-admission-receipt.v2"
    assert receipt["corpus"]["corpus_pre_admission_digest"] == _corpus_admission_receipt(dataset)["admission_digest"]
    binding = _corpus_admission_receipt(dataset)["corpus"]
    assert receipt["corpus_admission"]["dataset_binding_digest"] == binding["dataset_binding_digest"]
    assert receipt["corpus_admission"]["sealed_case_binding_digest"] == binding["sealed_case_binding_digest"]
    assert receipt["corpus_admission"]["sealed_support_binding_digest"] == binding["sealed_support_binding_digest"]
    assert receipt["corpus_admission"]["risk_counts"] == binding["risk_counts"]
    assert receipt["corpus_admission"]["sealed_risk_counts"] == binding["sealed_risk_counts"]
    assert verify_semantic_assurance_label_admission(receipt) == receipt


def test_v3_preserves_v6_corpus_profile_distribution_and_control_binding() -> None:
    dataset = _v2_dataset(v6=True)
    receipt = admit_semantic_assurance_human_labels(_v3_manifest(dataset), dataset)

    assert receipt["schema_version"] == "bhm.semantic-assurance-human-label-admission-receipt.v3"
    assert receipt["assurance"]["v6_corpus_structurally_bound"] is True
    binding = receipt["corpus_admission"]
    assert binding["receipt_schema_version"] == "bhm.semantic-assurance-corpus-admission-receipt.v2"
    assert binding["admission_profile"] == "v6_independent_human_curation"
    assert binding["partition_counts"] == {"development": 220, "sealed_holdout": 400, "validation": 220}
    assert all(all(count >= 1 for count in risks.values()) for risks in binding["partition_risk_counts"].values())
    assert len(binding["v6_control_binding_digest"]) == 64
    assert verify_semantic_assurance_label_admission(receipt) == receipt


def test_v3_rejects_v1_v2_corpus_admission_or_tampered_v6_distribution() -> None:
    dataset = _v2_dataset(v6=True)
    wrong_tier = _v3_manifest(dataset)
    wrong_tier["corpus_admission"] = _corpus_admission_receipt(dataset)
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="tier"):
        admit_semantic_assurance_human_labels(wrong_tier, dataset)

    tampered = _v3_manifest(dataset)
    corpus_admission = tampered["corpus_admission"]
    assert isinstance(corpus_admission, dict)
    corpus = corpus_admission["corpus"]
    assert isinstance(corpus, dict)
    partition_risks = corpus["partition_risk_counts"]
    assert isinstance(partition_risks, dict)
    partition_risks["development"]["factuality"] += 1
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="receipt"):
        admit_semantic_assurance_human_labels(tampered, dataset)


def test_v4_requires_exact_curator_audited_corpus_binding() -> None:
    dataset = _v2_dataset(v6=True)
    receipt = admit_semantic_assurance_human_labels(_v4_manifest(dataset), dataset)

    assert receipt["schema_version"] == "bhm.semantic-assurance-human-label-admission-receipt.v4"
    assert receipt["corpus_admission"]["receipt_schema_version"] == "bhm.semantic-assurance-corpus-admission-receipt.v3"
    assert receipt["corpus_admission"]["admission_profile"] == "v6_curator_audited_independent_human_curation"
    assert set(receipt["corpus_admission"]["curator_audit"]) == {
        "receipt_schema_version", "audit_digest", "atomic_fact_binding_digest",
        "control_binding_digest", "canonical_fragment_binding_digest",
    }
    assert receipt["assurance"]["v6_curator_audit_exactly_bound"] is True
    assert receipt["assurance"]["automatic_apply_ready"] is False
    assert receipt["execution"]["automatic_apply"] is False
    assert verify_semantic_assurance_label_admission(receipt) == receipt


def test_v4_remains_historical_compatibility_path_without_readiness_strata() -> None:
    dataset = _v2_dataset(v6=True)
    for case in dataset["cases"]:
        if case["partition"] == "sealed_holdout":
            case["expected"] = {
                "factuality": "true",
                "entailment": "supported",
                "conflict": "none",
                "operation": "no_op",
                "support_relevance": {support_id: "relevant" for support_id in case["support_ids"]},
            }

    receipt = admit_semantic_assurance_human_labels(_v4_manifest(dataset), dataset)

    assert receipt["schema_version"] == "bhm.semantic-assurance-human-label-admission-receipt.v4"
    assert "v6_sealed_exact_label_strata_bound" not in receipt["assurance"]
    assert verify_semantic_assurance_label_admission(receipt) == receipt


def test_v5_requires_sealed_exact_label_readiness_strata_and_returns_content_free_binding() -> None:
    dataset = _v2_dataset(v6=True)

    receipt = admit_semantic_assurance_human_labels(_v5_manifest(dataset), dataset)

    assert receipt["schema_version"] == "bhm.semantic-assurance-human-label-admission-receipt.v5"
    assert receipt["assurance"]["v6_sealed_exact_label_strata_bound"] is True
    assert len(receipt["assurance"]["sealed_readiness_strata_binding_digest"]) == 64
    assert receipt["assurance"]["sealed_readiness_strata_minimum_population"] == 80
    assert verify_semantic_assurance_label_admission(receipt) == receipt


def test_v6_requires_risk_label_matrix_and_aligned_conflict_cells() -> None:
    dataset = _v6_risk_matrix_dataset()

    receipt = admit_semantic_assurance_human_labels(_v6_manifest(dataset), dataset)

    assert receipt["schema_version"] == "bhm.semantic-assurance-human-label-admission-receipt.v6"
    assert receipt["assurance"]["v6_sealed_risk_label_matrix_bound"] is True
    assert len(receipt["assurance"]["sealed_risk_label_matrix_binding_digest"]) == 64
    assert receipt["assurance"]["sealed_risk_label_matrix_minimum_risk_classes"] == 2
    assert receipt["assurance"]["sealed_aligned_conflict_label_minimum_population"] == 50
    assert verify_semantic_assurance_label_admission(receipt) == receipt


def test_v6_rejects_global_v5_pass_with_misaligned_direct_conflicts() -> None:
    dataset = _v6_risk_matrix_dataset()
    cases = dataset["cases"]
    assert isinstance(cases, list)
    sealed = [case for case in cases if case["partition"] == "sealed_holdout"]
    for index in range(31):
        sealed[index]["risk_class"] = "factuality"
        sealed[320 + index]["risk_class"] = "direct_conflict"

    # The V5 global strata still pass; V6 detects that direct conflict evidence
    # has been relocated away from the declared direct-conflict risk class.
    assert admit_semantic_assurance_human_labels(_v5_manifest(dataset), dataset)["schema_version"].endswith(".v5")
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="aligned conflict"):
        admit_semantic_assurance_human_labels(_v6_manifest(dataset), dataset)


def test_v6_rejects_single_risk_concentration_despite_global_exact_floor() -> None:
    dataset = _v6_risk_matrix_dataset()
    cases = dataset["cases"]
    assert isinstance(cases, list)
    sealed = [case for case in cases if case["partition"] == "sealed_holdout"]
    for index in range(50, 80):
        sealed[index]["risk_class"] = "direct_conflict"
        sealed[320 + index - 50]["risk_class"] = "temporal_conflict"

    assert admit_semantic_assurance_human_labels(_v5_manifest(dataset), dataset)["schema_version"].endswith(".v5")
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="stratum diversity"):
        admit_semantic_assurance_human_labels(_v6_manifest(dataset), dataset)


def test_evaluator_recomputes_and_propagates_v6_risk_label_matrix() -> None:
    dataset = _v6_risk_matrix_dataset()
    admission = admit_semantic_assurance_human_labels(_v6_manifest(dataset), dataset)

    report = evaluate_semantic_assurance(dataset, _holdout_predictions(dataset), label_admission=admission)

    assert report["dataset"]["label_admission_version"] == "v6_v6_curator_audited_risk_matrix_bound"
    assert report["dataset"]["sealed_risk_label_matrix_binding_digest"] == (
        admission["assurance"]["sealed_risk_label_matrix_binding_digest"]
    )


def test_v5_readiness_strata_diagnostic_reports_content_free_gaps_without_admission() -> None:
    dataset = _v2_dataset(v6=True)
    for case in dataset["cases"]:
        if case["partition"] == "sealed_holdout":
            case["expected"] = {
                "factuality": "true",
                "entailment": "supported",
                "conflict": "none",
                "operation": "no_op",
                "support_relevance": {support_id: "relevant" for support_id in case["support_ids"]},
            }

    diagnostic = assess_semantic_assurance_v5_readiness_strata(_v4_manifest(dataset), dataset)

    assert diagnostic["schema_version"] == "bhm.semantic-assurance-v5-readiness-strata-diagnostic.v1"
    assert diagnostic["ok"] is True
    assert diagnostic["readiness_eligible"] is False
    assert diagnostic["minimum_population"] == 80
    assert diagnostic["strata"]["factuality"]["false"] == {"population": 0, "deficit": 80}
    assert diagnostic["strata"]["entailment"]["contradicted"] == {"population": 0, "deficit": 80}
    assert diagnostic["strata"]["support_relevance"]["irrelevant"] == {"population": 0, "deficit": 80}
    assert diagnostic["strata"]["conflict"]["implicit"] == {"population": 0, "deficit": 80}
    encoded = json.dumps(diagnostic, ensure_ascii=False)
    assert "opaque-" not in encoded
    assert "support-" not in encoded
    assert "reviewer" not in encoded
    assert diagnostic["assurance"]["automatic_apply_ready"] is False
    assert diagnostic["execution"] == {
        "network_used": False,
        "model_called": False,
        "sqlite_mutation": False,
        "mem0_mutation": False,
        "qdrant_mutation": False,
        "automatic_apply": False,
    }
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="exact-label strata"):
        admit_semantic_assurance_human_labels(_v5_manifest(dataset), dataset)


def test_v5_readiness_strata_diagnostic_is_eligible_only_for_exact_full_population() -> None:
    dataset = _v2_dataset(v6=True)

    diagnostic = assess_semantic_assurance_v5_readiness_strata(_v5_manifest(dataset), dataset)

    assert diagnostic["readiness_eligible"] is True
    assert all(
        item["deficit"] == 0
        for dimension in diagnostic["strata"].values()
        for item in dimension.values()
    )
    assert diagnostic["bindings"]["sealed_readiness_strata_binding_digest"] == (
        admit_semantic_assurance_human_labels(_v5_manifest(dataset), dataset)["assurance"]
        ["sealed_readiness_strata_binding_digest"]
    )


def test_v5_readiness_strata_diagnostic_rejects_tampered_adjudication_binding() -> None:
    dataset = _v2_dataset(v6=True)
    manifest = _v4_manifest(dataset)
    adjudications = manifest["adjudications"]
    assert isinstance(adjudications, list)
    labels = adjudications[0]["labels"]
    assert isinstance(labels, dict)
    adjudications[0]["labels"] = {**labels, "conflict": "direct"}

    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="do not exactly bind"):
        assess_semantic_assurance_v5_readiness_strata(manifest, dataset)


@pytest.mark.parametrize(("dimension", "label", "replacement"), [
    ("factuality", "false", "true"),
    ("factuality", "ambiguous", "true"),
    ("factuality", "insufficient", "true"),
    ("entailment", "contradicted", "supported"),
    ("entailment", "insufficient", "supported"),
    ("support_relevance", "irrelevant", "relevant"),
    ("conflict", "direct", "none"),
    ("conflict", "temporal", "none"),
    ("conflict", "scope", "none"),
    ("conflict", "implicit", "none"),
])
def test_v5_rejects_each_underpopulated_sealed_exact_label_stratum(
    dimension: str,
    label: str,
    replacement: str,
) -> None:
    dataset = _v2_dataset(v6=True)
    cases = dataset["cases"]
    assert isinstance(cases, list)
    for case in cases:
        if case["partition"] != "sealed_holdout":
            continue
        expected = case["expected"]
        assert isinstance(expected, dict)
        if dimension == "support_relevance":
            relevance = expected[dimension]
            assert isinstance(relevance, dict)
            support_id = next((key for key, value in relevance.items() if value == label), None)
            if support_id is not None:
                relevance[support_id] = replacement
                break
        elif expected[dimension] == label:
            expected[dimension] = replacement
            break
    else:
        raise AssertionError(f"missing V5 stratum fixture {dimension}.{label}")

    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="exact-label strata"):
        admit_semantic_assurance_human_labels(_v5_manifest(dataset), dataset)


@pytest.mark.parametrize(("field", "value"), [
    ("v6_sealed_exact_label_strata_bound", False),
    ("sealed_readiness_strata_binding_digest", "0" * 64),
    ("sealed_readiness_strata_minimum_population", 79),
])
def test_v5_receipt_rejects_tampered_sealed_readiness_strata_binding(field: str, value: object) -> None:
    dataset = _v2_dataset(v6=True)
    receipt = admit_semantic_assurance_human_labels(_v5_manifest(dataset), dataset)
    tampered = {**receipt, "assurance": {**receipt["assurance"], field: value}}

    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="receipt boundary|receipt digest"):
        verify_semantic_assurance_label_admission(tampered)


@pytest.mark.parametrize("mutate", [
    lambda manifest: manifest["corpus_admission"].__setitem__("schema_version", "bhm.semantic-assurance-corpus-admission-receipt.v2"),
    lambda manifest: manifest["corpus_admission"].__setitem__("admission_profile", "v6_independent_human_curation"),
    lambda manifest: manifest["corpus_admission"]["curator_audit"].__setitem__("audit_digest", "0" * 64),
    lambda manifest: manifest["corpus_admission"]["curator_audit"].__setitem__("unexpected", "0" * 64),
])
def test_v4_rejects_downgrade_or_tampered_curator_binding(mutate) -> None:
    dataset = _v2_dataset(v6=True)
    manifest = _v4_manifest(dataset)
    mutate(manifest)
    with pytest.raises(SemanticAssuranceLabelAdmissionError):
        admit_semantic_assurance_human_labels(manifest, dataset)


def test_v1_cannot_masquerade_as_corpus_admitted_v2() -> None:
    dataset = _v2_dataset()
    v1_receipt = admit_semantic_assurance_human_labels(_manifest(dataset), dataset)
    assert v1_receipt["schema_version"] == "bhm.semantic-assurance-human-label-admission-receipt.v1"
    assert "corpus_admission" not in v1_receipt

    fake_v2 = {**v1_receipt, "schema_version": "bhm.semantic-assurance-human-label-admission-receipt.v2"}
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="receipt schema"):
        verify_semantic_assurance_label_admission(fake_v2)


def test_v2_rejects_missing_tampered_or_cardinality_mismatched_corpus_admission() -> None:
    dataset = _v2_dataset()
    missing = _manifest(dataset)
    missing["schema_version"] = "bhm.semantic-assurance-human-label-admission.v2"
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="unexpected fields"):
        admit_semantic_assurance_human_labels(missing, dataset)

    tampered = _v2_manifest(dataset)
    corpus_admission = tampered["corpus_admission"]
    assert isinstance(corpus_admission, dict)
    corpus_admission["corpus"] = {**corpus_admission["corpus"], "case_count": 559}
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="corpus admission receipt"):
        admit_semantic_assurance_human_labels(tampered, dataset)

    mismatched = _v2_manifest(dataset)
    mismatched_receipt = mismatched["corpus_admission"]
    assert isinstance(mismatched_receipt, dict)
    mismatched_core = {key: value for key, value in mismatched_receipt.items() if key != "admission_digest"}
    mismatched_core["corpus"] = {
        **mismatched_core["corpus"],
        "case_count": 561,
        "partition_counts": {"development": 80, "sealed_holdout": 400, "validation": 81},
    }
    mismatched_core["review"] = {**mismatched_core["review"], "packet_case_count": 561}
    mismatched["corpus_admission"] = {**mismatched_core, "admission_digest": _canonical_digest(mismatched_core)}
    with pytest.raises(SemanticAssuranceLabelAdmissionError, match="corpus admission receipt"):
        admit_semantic_assurance_human_labels(mismatched, dataset)
