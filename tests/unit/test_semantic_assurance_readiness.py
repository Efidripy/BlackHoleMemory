from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from blackholememory.semantic_assurance_evaluation import evaluate_semantic_assurance
from blackholememory.semantic_assurance_evaluation import load_semantic_assurance_dataset
from blackholememory.semantic_assurance_label_admission import admit_semantic_assurance_human_labels
from blackholememory.semantic_assurance_readiness import SemanticAssuranceReadinessError
from blackholememory.semantic_assurance_readiness import assess_semantic_assurance_readiness
from blackholememory.semantic_assurance_readiness import load_semantic_assurance_readiness


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "semantic-assurance-v1.json"
ALL_RISKS = [
    "factuality", "entailment", "relevance", "direct_conflict", "temporal_conflict",
    "scope_conflict", "implicit_conflict", "injection", "cross_project",
    "retrieval_pressure", "abstention",
]
SEALED_RISKS = [
    "direct_conflict", "temporal_conflict", "scope_conflict", "implicit_conflict",
    "injection", "cross_project", "retrieval_pressure", "abstention",
]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _human_dataset(tmp_path: Path) -> dict[str, object]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["labeling"] = {"kind": "independent_human_review"}
    raw["label_provenance"] = {"kind": "independent_human_review"}
    sealed_index = 0
    for index, case in enumerate(raw["cases"]):
        partition = case["partition"]
        case["source_group_id"] = f"source-{partition}-{case['group_id']}"
        case["source_theme"] = f"theme-{partition}-{case['family_id']}"
        case["prompt_template_id"] = f"template-{partition}-{case['case_id']}"
        case["paraphrase_cluster_id"] = f"paraphrase-{partition}-{case['case_id']}"
        if partition == "sealed_holdout":
            case["risk_class"] = SEALED_RISKS[sealed_index]
            sealed_index += 1
        else:
            case["risk_class"] = ALL_RISKS[index % len(ALL_RISKS)]
    path = tmp_path / "independent-human-labels.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_semantic_assurance_dataset(path)


def _policy() -> dict[str, object]:
    return {
        "schema_version": "bhm.semantic-assurance-threshold-policy.v1",
        "policy_id": "unit-policy-v1",
        "minimum_case_count": 1,
        "minimum_support_row_count": 1,
        "minimum_repeats": 2,
        "minimum_accuracy_lower_95": {
            "factuality": 0.1,
            "entailment": 0.1,
            "support_relevance": 0.1,
        },
        "minimum_conflict_recall_lower_95": 0.1,
        "unsafe_non_no_op_budget": 0,
        "missed_conflict_budget": 0,
        "required_risk_classes": ALL_RISKS,
    }


def _sealed_digest(dataset: dict[str, object]) -> str:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    sealed = [
        {"case_id": case["case_id"], "isolation": case["isolation"], "risk_class": case["risk_class"], "expected": case["expected"]}
        for case in cases
        if case["partition"] == "sealed_holdout"
    ]
    return _digest(sorted(sealed, key=lambda item: str(item["case_id"])))


def _admission(dataset: dict[str, object], policy: dict[str, object]) -> dict[str, object]:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    labels = [{"case_id": case["case_id"], "labels": case["expected"]} for case in cases]
    manifest = {
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
            "protocol_id": "readiness-unit-protocol",
            "committed_at": "2026-10-18T00:00:00Z",
            "sealed_holdout_commitment_digest": _sealed_digest(dataset),
            "threshold_policy_digest": _digest(policy),
            "tuning_scope": "development_only",
            "validation_is_frozen": True,
            "sealed_holdout_immutable": True,
            "labels_excluded_from_model_input": True,
            "required_risk_classes": ALL_RISKS,
        },
        "reviewers": [
            {"reviewer_id_digest": _digest("reviewer-a"), "labels": labels},
            {"reviewer_id_digest": _digest("reviewer-b"), "labels": labels},
        ],
        "adjudications": [
            {"case_id": row["case_id"], "adjudicator_id_digest": _digest("adjudicator"), "labels": row["labels"]}
            for row in labels
        ],
    }
    return admit_semantic_assurance_human_labels(manifest, dataset)


def _live_receipt(dataset: dict[str, object], admission: dict[str, object]) -> dict[str, object]:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    predictions = [{"case_id": case["case_id"], **case["expected"]} for case in cases if case["partition"] == "sealed_holdout"]
    prompt_digest = _digest("bhm.semantic-assurance-live-classifier.v1")
    report = evaluate_semantic_assurance(
        dataset,
        predictions,
        label_admission=admission,
        invocation={
            "model_id": "unit/exact-local-model",
            "model_variant": "unit/exact-local-model@q6",
            "prompt_digest": prompt_digest,
            "endpoint": "http://127.0.0.1:13666",
        },
    )
    core = {
        "schema_version": "bhm.semantic-assurance-live-receipt.v1",
        "mode": "live-read-only-local-classifier",
        "dataset": {key: dataset[key] for key in ("dataset_id", "fixture_digest", "label_digest")},
        "model": {
            "model_id": "unit/exact-local-model",
            "model_variant": "unit/exact-local-model@q6",
            "endpoint": "http://127.0.0.1:13666",
        },
        "prompt_contract": {"name": "bhm.semantic-assurance-live-classifier.v1", "digest": prompt_digest},
        "execution": {
            "partition": "sealed_holdout", "repeats_requested": 2, "repeats_completed": 2,
            "chat_call_count": len(predictions) * 2, "temperature": 0, "reasoning": "off", "store": False,
            "allowed_http_routes": ["GET /api/v1/models", "POST /api/v1/chat"],
            "model_load_requested": False, "model_unload_requested": False, "model_download_requested": False,
            "model_configuration_requested": False, "model_load_telemetry": "available",
            "zero_model_load_events_when_observed": True, "sqlite_mutation": False, "mem0_mutation": False,
            "qdrant_mutation": False, "mcp_or_tools_used": False, "queue_persistence": False,
            "automatic_apply": False, "elapsed_ms": 1.0,
        },
        "inventory": {
            "before": {"exact_requested_model_loaded": True, "only_requested_llm_loaded": True, "loaded_llm_count": 1},
            "after": {"exact_requested_model_loaded": True, "only_requested_llm_loaded": True, "loaded_llm_count": 1},
        },
        "model_load_events": 0,
        "model_load_telemetry": "available",
        "offline_reports": [report, report],
        "failure": None,
        "gate": {
            "checks": {
                "exact_requested_model_loaded_before": True, "exact_requested_model_loaded_after": True,
                "only_requested_llm_loaded_before": True, "only_requested_llm_loaded_after": True,
                "all_classifications_parsed": True, "offline_mechanical_valid": True,
                "deterministic_repeat_agreement": True, "model_boundaries_intact": True,
            },
            "mechanical_valid": True, "accepted": True,
            "acceptance_scope": "proposal_only_human_labelled_measurement",
            "automatic_apply_ready": False, "automatic_apply_decision": "no_go",
        },
        "limitations": ["unit-only content-free live receipt"],
    }
    return {**core, "report_digest": _digest(core)}


def _evidence(tmp_path: Path) -> dict[str, object]:
    dataset = _human_dataset(tmp_path)
    policy = _policy()
    admission = _admission(dataset, policy)
    live = _live_receipt(dataset, admission)
    evaluated_run_digest = _digest({
        "live_report_digest": live["report_digest"],
        "report_digest": live["offline_reports"][0]["report_digest"],
        "admission_digest": admission["admission_digest"],
    })
    model_run_identity_digest = _digest({
        "model": dict(sorted(live["model"].items())),
        "prompt_contract": live["prompt_contract"],
        "partition": "sealed_holdout", "repeats": 2,
        "chat_call_count": live["execution"]["chat_call_count"],
    })
    risk_core = {
        "schema_version": "bhm.semantic-assurance-risk-coverage-receipt.v1",
        "admission_digest": admission["admission_digest"],
        "evaluated_run_digest": evaluated_run_digest,
        "all_risk_classes": ALL_RISKS,
        "sealed_holdout_risk_classes": SEALED_RISKS,
    }
    shadow_core = {
        "schema_version": "bhm.semantic-assurance-shadow-human-oracle-receipt.v1",
        "campaign_id_digest": _digest("unit-shadow"), "admission_digest": admission["admission_digest"],
        "evaluated_run_digest": evaluated_run_digest, "case_count": 8, "human_oracle_case_count": 8,
        "disagreement_count": 0, "unresolved_disagreement_count": 0,
        "execution": {"read_only": True, "model_called": False, "sqlite_mutation": False, "mem0_mutation": False, "qdrant_mutation": False, "automatic_apply": False},
    }
    rehearsal_core = {
        "schema_version": "bhm.semantic-assurance-disposable-sqlite-rehearsal-receipt.v1",
        "rehearsal_id_digest": _digest("unit-rehearsal"), "admission_digest": admission["admission_digest"],
        "evaluated_run_digest": evaluated_run_digest,
        "execution": {"disposable_sqlite_only": True, "fault_injected": True, "rollback_verified": True, "outbox_rollback_verified": True, "production_sqlite_touched": False, "mem0_mutation": False, "qdrant_mutation": False, "automatic_apply": False},
    }
    charter_core = {
        "schema_version": "bhm.semantic-assurance-owner-charter-binding.v1",
        "charter_id_digest": _digest("unit-owner-charter"), "approved_at": "2026-10-18T00:00:00Z",
        "admission_digest": admission["admission_digest"], "evaluated_run_digest": evaluated_run_digest,
        "model_run_identity_digest": model_run_identity_digest,
        "threshold_policy_digest": _digest(policy), "readiness_aggregation_authorized": True,
        "automatic_apply_authorized": False,
    }
    return {
        "schema_version": "bhm.semantic-assurance-readiness.v1",
        "admission_receipt": admission,
        "live_receipt": live,
        "threshold_policy": policy,
        "risk_coverage": {**risk_core, "receipt_digest": _digest(risk_core)},
        "shadow_human_oracle": {**shadow_core, "receipt_digest": _digest(shadow_core)},
        "sqlite_rehearsal": {**rehearsal_core, "receipt_digest": _digest(rehearsal_core)},
        "architecture_invariants": {
            "sqlite_authoritative": True, "mem0_semantic_logical_layer": True,
            "qdrant_rebuildable_projection": True, "langgraph_orchestration": True,
            "direct_mem0_qdrant_writes_prohibited": True, "automatic_semantic_apply_no_go": True,
        },
        "owner_charter": {**charter_core, "charter_digest": _digest(charter_core)},
    }


def test_complete_evidence_remains_no_go_and_is_content_free(tmp_path: Path) -> None:
    receipt = assess_semantic_assurance_readiness(_evidence(tmp_path))

    assert receipt["status"] == "evidence_complete_but_release_not_authorized"
    assert receipt["decision"] == {
        "automatic_apply_ready": False,
        "decision": "no_go",
        "reason": "readiness evidence aggregation is not deployment authorization",
    }
    rendered = json.dumps(receipt, sort_keys=True)
    assert "unit/exact-local-model" not in rendered
    assert "127.0.0.1" not in rendered
    assert "reviewer-a" not in rendered


def test_rejects_tampered_admission_receipt(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    admission = evidence["admission_receipt"]
    assert isinstance(admission, dict)
    admission["review"]["case_count"] = 99

    with pytest.raises(SemanticAssuranceReadinessError, match="admission receipt"):
        assess_semantic_assurance_readiness(evidence)


def test_rejects_missing_risk_class(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    risk_coverage = evidence["risk_coverage"]
    assert isinstance(risk_coverage, dict)
    risk_coverage["all_risk_classes"] = ALL_RISKS[:-1]
    risk_core = {key: value for key, value in risk_coverage.items() if key != "receipt_digest"}
    risk_coverage["receipt_digest"] = _digest(risk_core)

    with pytest.raises(SemanticAssuranceReadinessError, match="risk coverage taxonomy"):
        assess_semantic_assurance_readiness(evidence)


def test_rejects_lower_confidence_bound_below_policy(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    policy = evidence["threshold_policy"]
    assert isinstance(policy, dict)
    policy["minimum_accuracy_lower_95"]["factuality"] = 0.99
    admission = evidence["admission_receipt"]
    assert isinstance(admission, dict)
    admission["pre_registration"]["threshold_policy_digest"] = _digest(policy)
    admission_core = {key: value for key, value in admission.items() if key != "admission_digest"}
    admission["admission_digest"] = _digest(admission_core)
    # Every dependent binding must remain structurally exact so the metric gate is reached.
    _rebind_admission_in_live(evidence)
    _rebind_dependents(evidence)

    with pytest.raises(SemanticAssuranceReadinessError, match="lower confidence bound"):
        assess_semantic_assurance_readiness(evidence)


def test_rejects_unsafe_or_invalid_shadow_and_unknown_field(tmp_path: Path) -> None:
    unsafe = _evidence(tmp_path)
    report = unsafe["live_receipt"]["offline_reports"][0]
    report["safety"]["unsafe_non_no_op_count"] = 1
    _rebind_live(unsafe)
    _rebind_dependents(unsafe)
    with pytest.raises(SemanticAssuranceReadinessError, match="unsafe"):
        assess_semantic_assurance_readiness(unsafe)

    invalid_shadow = _evidence(tmp_path)
    invalid_shadow["shadow_human_oracle"]["execution"]["sqlite_mutation"] = True
    shadow = invalid_shadow["shadow_human_oracle"]
    shadow["receipt_digest"] = _digest({key: value for key, value in shadow.items() if key != "receipt_digest"})
    with pytest.raises(SemanticAssuranceReadinessError, match="not read-only"):
        assess_semantic_assurance_readiness(invalid_shadow)

    invalid_rehearsal = _evidence(tmp_path)
    invalid_rehearsal["sqlite_rehearsal"]["execution"]["rollback_verified"] = False
    rehearsal = invalid_rehearsal["sqlite_rehearsal"]
    rehearsal["receipt_digest"] = _digest({key: value for key, value in rehearsal.items() if key != "receipt_digest"})
    with pytest.raises(SemanticAssuranceReadinessError, match="SQLite rehearsal"):
        assess_semantic_assurance_readiness(invalid_rehearsal)

    unknown = _evidence(tmp_path)
    unknown["surprise"] = True
    with pytest.raises(SemanticAssuranceReadinessError, match="unexpected fields"):
        assess_semantic_assurance_readiness(unknown)


def test_loader_reads_only_local_json(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    path = tmp_path / "readiness.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    assert load_semantic_assurance_readiness(path)["decision"]["decision"] == "no_go"


def _rebind_live(evidence: dict[str, object]) -> None:
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    reports = live["offline_reports"]
    assert isinstance(reports, list)
    reports[1] = reports[0]
    report = reports[0]
    assert isinstance(report, dict)
    report["report_digest"] = _digest({key: value for key, value in report.items() if key != "report_digest"})
    live["report_digest"] = _digest({key: value for key, value in live.items() if key != "report_digest"})


def _rebind_admission_in_live(evidence: dict[str, object]) -> None:
    admission = evidence["admission_receipt"]
    live = evidence["live_receipt"]
    assert isinstance(admission, dict) and isinstance(live, dict)
    reports = live["offline_reports"]
    assert isinstance(reports, list)
    report = reports[0]
    assert isinstance(report, dict)
    report["dataset"]["human_label_admission_digest"] = admission["admission_digest"]
    report["report_digest"] = _digest({key: value for key, value in report.items() if key != "report_digest"})
    reports[1] = report
    live["report_digest"] = _digest({key: value for key, value in live.items() if key != "report_digest"})


def _rebind_dependents(evidence: dict[str, object]) -> None:
    admission = evidence["admission_receipt"]
    live = evidence["live_receipt"]
    policy = evidence["threshold_policy"]
    assert isinstance(admission, dict) and isinstance(live, dict) and isinstance(policy, dict)
    live_report = live["offline_reports"][0]
    evaluated_run_digest = _digest({"live_report_digest": live["report_digest"], "report_digest": live_report["report_digest"], "admission_digest": admission["admission_digest"]})
    model_run_identity_digest = _digest({"model": dict(sorted(live["model"].items())), "prompt_contract": live["prompt_contract"], "partition": "sealed_holdout", "repeats": 2, "chat_call_count": live["execution"]["chat_call_count"]})
    for name in ("risk_coverage", "shadow_human_oracle", "sqlite_rehearsal"):
        item = evidence[name]
        assert isinstance(item, dict)
        item["admission_digest"] = admission["admission_digest"]
        item["evaluated_run_digest"] = evaluated_run_digest
        item["receipt_digest"] = _digest({key: value for key, value in item.items() if key != "receipt_digest"})
    charter = evidence["owner_charter"]
    assert isinstance(charter, dict)
    charter["admission_digest"] = admission["admission_digest"]
    charter["evaluated_run_digest"] = evaluated_run_digest
    charter["model_run_identity_digest"] = model_run_identity_digest
    charter["threshold_policy_digest"] = _digest(policy)
    charter["charter_digest"] = _digest({key: value for key, value in charter.items() if key != "charter_digest"})
