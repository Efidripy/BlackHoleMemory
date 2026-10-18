from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from blackholememory.semantic_assurance_evaluation import evaluate_semantic_assurance
from blackholememory.semantic_assurance_evaluation import load_semantic_assurance_dataset
from blackholememory.semantic_assurance_label_admission import SemanticAssuranceLabelAdmissionError
from blackholememory.semantic_assurance_label_admission import admit_semantic_assurance_human_labels
from blackholememory.semantic_assurance_label_admission import verify_semantic_assurance_label_admission


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "semantic-assurance-v1.json"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _human_dataset(tmp_path: Path) -> dict[str, object]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["labeling"] = {"kind": "independent_human_review"}
    raw["label_provenance"] = {"kind": "independent_human_review"}
    for case in raw["cases"]:
        partition = case["partition"]
        case["source_group_id"] = f"source-{partition}-{case['group_id']}"
        case["source_theme"] = f"theme-{partition}-{case['family_id']}"
        case["prompt_template_id"] = f"template-{partition}-{case['case_id']}"
        case["paraphrase_cluster_id"] = f"paraphrase-{partition}-{case['case_id']}"
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
        "reviewers": [
            {"reviewer_id_digest": _digest("reviewer-a"), "labels": label_rows},
            {"reviewer_id_digest": _digest("reviewer-b"), "labels": label_rows},
        ],
        "adjudications": [
            {"case_id": row["case_id"], "adjudicator_id_digest": _digest("adjudicator"), "labels": row["labels"]}
            for row in label_rows
        ],
    }


def _holdout_predictions(dataset: dict[str, object]) -> list[dict[str, object]]:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    return [
        {"case_id": case["case_id"], **case["expected"]}
        for case in cases
        if case["partition"] == "sealed_holdout"
    ]


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
    assert admitted["gate"]["safety_case_eligible"] is True
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
