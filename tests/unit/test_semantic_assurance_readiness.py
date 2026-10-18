from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from blackholememory.semantic_assurance_evaluation import evaluate_semantic_assurance
from blackholememory.semantic_assurance_evaluation import recompute_semantic_assurance_results
from blackholememory.semantic_assurance_corpus_admission import verify_semantic_assurance_corpus_admission
from blackholememory.semantic_assurance_label_admission import admit_semantic_assurance_human_labels
from blackholememory.semantic_assurance_readiness import SemanticAssuranceReadinessError
from blackholememory.semantic_assurance_readiness import assess_semantic_assurance_readiness
from blackholememory.semantic_assurance_readiness import load_semantic_assurance_readiness
from blackholememory.semantic_assurance_live import DEFAULT_BASE_URL, DEFAULT_MODEL_ID, DEFAULT_MODEL_VARIANT, PROMPT_CONTRACT


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


def _structural_unit_dataset() -> dict[str, object]:
    """Build only a synthetic structural fixture, never human/model evidence."""

    cases: list[dict[str, object]] = []
    sealed_risks = [risk for risk in SEALED_RISKS for _ in range(50)]
    sequence = 0
    for partition, count in (("development", 80), ("validation", 80), ("sealed_holdout", 400)):
        for offset in range(count):
            case_id = f"opaque-{sequence:04d}"
            support_ids = (
                f"support-{sequence:04d}-a",
                f"support-{sequence:04d}-b",
            ) if partition == "sealed_holdout" else (f"support-{sequence:04d}",)
            cases.append({
                "case_id": case_id,
                "partition": partition,
                "support_ids": support_ids,
                "risk_class": sealed_risks[offset] if partition == "sealed_holdout" else ALL_RISKS[offset % len(ALL_RISKS)],
                "expected": {
                    "factuality": "true",
                    "entailment": "supported",
                    "conflict": "direct" if partition == "sealed_holdout" and offset < 200 else "none",
                    "operation": "no_op",
                    "support_relevance": {support_id: "relevant" for support_id in support_ids},
                },
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
        "dataset_id": "synthetic-v2-readiness-structural-fixture",
        "fixture_digest": _digest("synthetic-v2-fixture"),
        "label_digest": _digest("synthetic-v2-labels"),
        "labeling_kind": "independent_human_review",
        "label_provenance_kind": "independent_human_review",
        "quality_gate": {
            "unsafe_non_no_op_budget": 0,
            "conflict_recall_required": 1.0,
            "unsupported_or_ambiguous_operation": "no_op",
            "automatic_apply_decision": "no_go",
        },
        "cases": cases,
    }


def _policy() -> dict[str, object]:
    return {
        "schema_version": "bhm.semantic-assurance-threshold-policy.v1",
        "policy_id": "unit-policy-v1",
        "minimum_case_count": 400,
        "minimum_support_row_count": 800,
        "minimum_repeats": 3,
        "minimum_accuracy_lower_95": {
            "factuality": 0.95,
            "entailment": 0.95,
            "support_relevance": 0.95,
        },
        "minimum_conflict_recall_lower_95": 0.95,
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


def _admission(
    dataset: dict[str, object],
    policy: dict[str, object],
    *,
    corpus_admitted: bool = True,
) -> dict[str, object]:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    labels = [{"case_id": case["case_id"], "labels": case["expected"]} for case in cases]
    manifest = {
        "schema_version": "bhm.semantic-assurance-human-label-admission.v2" if corpus_admitted else "bhm.semantic-assurance-human-label-admission.v1",
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
    if corpus_admitted:
        manifest["corpus_admission"] = _corpus_admission_receipt(dataset)
    return admit_semantic_assurance_human_labels(manifest, dataset)


def _corpus_admission_receipt(dataset: dict[str, object]) -> dict[str, object]:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    partition_counts = {
        partition: sum(case["partition"] == partition for case in cases)
        for partition in ("development", "validation", "sealed_holdout")
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
    risk_counts = {risk: sum(item["risk_class"] == risk for item in rows) for risk in set(ALL_RISKS)}
    sealed_risk_counts = {risk: sum(item["risk_class"] == risk for item in sealed_rows) for risk in set(ALL_RISKS)}
    core = {
        "schema_version": "bhm.semantic-assurance-corpus-admission-receipt.v1",
        "ok": True,
        "corpus": {
            "case_count": len(cases),
            "partition_counts": partition_counts,
            "sealed_support_count": sealed_support_count,
            "dataset_binding_digest": _digest(rows),
            "sealed_case_binding_digest": _digest(sealed_rows),
            "sealed_support_binding_digest": _digest([{"case_id": item["case_id"], "support_ids": item["support_ids"]} for item in sealed_rows]),
            "risk_counts": risk_counts,
            "sealed_risk_counts": sealed_risk_counts,
            "packet_payload_set_digest": _digest("synthetic-v2-packet-payload-set"),
            "curator_ledger_digest": _digest("synthetic-v2-curator-ledger"),
            "curator_attestation_digest": _digest("synthetic-v2-curator-attestation"),
        },
        "review": {
            "reviewer_packet_count": 2,
            "packet_case_count": len(cases),
            "same_payload_digest_set": True,
            "same_packet_positions": 0,
            "blank_ledger_coverage": True,
        },
        "assurance": {
            "status": "structurally_admitted_for_human_review_not_semantic_independence_proven",
            "curator_claimed_attestation": True,
            "curator_attestation_bound_to_corpus": True,
            "curator_identity_externally_authenticated": False,
            "semantic_independence_proven": False,
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
    receipt = {**core, "admission_digest": _digest(core)}
    assert verify_semantic_assurance_corpus_admission(receipt) == receipt
    return receipt


def _live_receipt(dataset: dict[str, object], admission: dict[str, object]) -> dict[str, object]:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    predictions = [{"case_id": case["case_id"], **case["expected"]} for case in cases if case["partition"] == "sealed_holdout"]
    prompt_digest = _digest(PROMPT_CONTRACT)
    reports = [evaluate_semantic_assurance(
        dataset, predictions, label_admission=admission,
        invocation={"model_id": DEFAULT_MODEL_ID, "model_variant": DEFAULT_MODEL_VARIANT, "prompt_digest": prompt_digest, "endpoint": DEFAULT_BASE_URL, "repeat_number": repeat, "case_count": 400, "case_id_set_digest": _corpus_admission_receipt(dataset)["corpus"]["sealed_case_binding_digest"]},
    ) for repeat in (1, 2, 3)]
    repeat_executions = []
    for repeat, report in enumerate(reports, start=1):
        entry = {"repeat_number": repeat, "case_count": 400, "case_id_set_digest": _corpus_admission_receipt(dataset)["corpus"]["sealed_case_binding_digest"], "chat_call_count": 400, "prediction_digest": report["prediction_digest"]}
        repeat_executions.append({**entry, "execution_digest": _digest(entry)})
    core = {
        "schema_version": "bhm.semantic-assurance-live-receipt.v1",
        "mode": "live-read-only-local-classifier",
        "dataset": {key: dataset[key] for key in ("dataset_id", "fixture_digest", "label_digest")},
        "model": {
            "model_id": DEFAULT_MODEL_ID,
            "model_variant": DEFAULT_MODEL_VARIANT,
            "endpoint": DEFAULT_BASE_URL,
        },
        "prompt_contract": {"name": "bhm.semantic-assurance-live-classifier.v1", "digest": prompt_digest},
        "execution": {
            "partition": "sealed_holdout", "repeats_requested": 3, "repeats_completed": 3,
            "chat_call_count": 400 * 3, "temperature": 0, "reasoning": "off", "store": False,
            "allowed_http_routes": ["GET /api/v1/models", "POST /api/v1/chat"],
            "model_load_requested": False, "model_unload_requested": False, "model_download_requested": False,
            "model_configuration_requested": False, "model_load_telemetry": "available",
            "zero_model_load_events_when_observed": True, "sqlite_mutation": False, "mem0_mutation": False,
            "qdrant_mutation": False, "mcp_or_tools_used": False, "queue_persistence": False,
            "automatic_apply": False, "elapsed_ms": 1.0, "repeat_executions": repeat_executions,
        },
        "inventory": {
            "before": {"exact_requested_model_loaded": True, "only_requested_llm_loaded": True, "loaded_llm_count": 1},
            "after": {"exact_requested_model_loaded": True, "only_requested_llm_loaded": True, "loaded_llm_count": 1},
        },
        "model_load_events": 0,
        "model_load_telemetry": "available",
        "offline_reports": reports,
        "failure": None,
        "gate": {
            "checks": {
                "exact_requested_model_loaded_before": True, "exact_requested_model_loaded_after": True,
                "only_requested_llm_loaded_before": True, "only_requested_llm_loaded_after": True,
                "all_classifications_parsed": True, "offline_mechanical_valid": True,
                "deterministic_repeat_agreement": True, "exact_repeat_execution_coverage": True, "model_boundaries_intact": True,
            },
            "mechanical_valid": True, "accepted": True,
            "acceptance_scope": "proposal_only_human_labelled_measurement",
            "automatic_apply_ready": False, "automatic_apply_decision": "no_go",
        },
        "limitations": ["unit-only synthetic structural fixture; not human, corpus, or model-run evidence"],
    }
    return {**core, "report_digest": _digest(core)}


def _evidence(tmp_path: Path) -> dict[str, object]:
    del tmp_path
    dataset = _structural_unit_dataset()
    policy = _policy()
    admission = _admission(dataset, policy)
    live = _live_receipt(dataset, admission)
    evaluated_run_digest = _evaluated_run_digest(live, admission)
    model_run_identity_digest = _digest({
        "model": dict(sorted(live["model"].items())),
        "prompt_contract": live["prompt_contract"],
        "partition": "sealed_holdout", "repeats": 3,
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
        "evaluated_run_digest": evaluated_run_digest, "case_count": 400, "human_oracle_case_count": 400,
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
    assert DEFAULT_MODEL_ID not in rendered
    assert "127.0.0.1" not in rendered
    assert "reviewer-a" not in rendered


def test_rejects_v1_label_admission_without_corpus_pre_admission(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    policy = evidence["threshold_policy"]
    assert isinstance(policy, dict)
    evidence["admission_receipt"] = _admission(
        _structural_unit_dataset(),
        policy,
        corpus_admitted=False,
    )

    with pytest.raises(SemanticAssuranceReadinessError, match="v2 corpus-admitted"):
        assess_semantic_assurance_readiness(evidence)


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
    policy["minimum_accuracy_lower_95"]["factuality"] = 0.991
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("minimum_case_count", 399),
        ("minimum_support_row_count", 799),
        ("minimum_repeats", 2),
        ("minimum_conflict_recall_lower_95", 0.949999),
    ],
)
def test_rejects_policy_below_sealed_safety_case_floors(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    evidence = _evidence(tmp_path)
    policy = evidence["threshold_policy"]
    assert isinstance(policy, dict)
    policy[field] = value

    with pytest.raises(SemanticAssuranceReadinessError, match="sealed safety-case minima"):
        assess_semantic_assurance_readiness(evidence)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_count", 399),
        ("support_row_count", 799),
    ],
)
def test_rejects_subminimum_sealed_measurement(
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    evidence = _evidence(tmp_path)
    report = evidence["live_receipt"]["offline_reports"][0]
    assert isinstance(report, dict)
    report["counts"][field] = value
    if field == "case_count":
        report["counts"]["exact_case_match_count"] = value
    _rebind_live(evidence)
    _rebind_dependents(evidence)

    with pytest.raises(SemanticAssuranceReadinessError, match="offline aggregates do not match canonical results"):
        assess_semantic_assurance_readiness(evidence)


def test_rejects_two_repeats_even_with_other_sealed_minima(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    execution = live["execution"]
    reports = live["offline_reports"]
    assert isinstance(execution, dict) and isinstance(reports, list)
    execution["repeats_requested"] = 2
    execution["repeats_completed"] = 2
    execution["chat_call_count"] = 400 * 2
    del reports[2:]
    _rebind_live(evidence)
    _rebind_dependents(evidence)

    with pytest.raises(SemanticAssuranceReadinessError, match="live execution coverage is invalid"):
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


def test_rejects_verbatim_copied_repeat_report(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    reports = live["offline_reports"]
    assert isinstance(reports, list)
    reports[1] = deepcopy(reports[0])
    _rebind_live(evidence)

    with pytest.raises(SemanticAssuranceReadinessError, match="repeat invocation binding"):
        assess_semantic_assurance_readiness(evidence)


def test_rejects_duplicate_or_missing_repeat_number(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    execution = live["execution"]
    assert isinstance(execution, dict)
    ledger = execution["repeat_executions"]
    assert isinstance(ledger, list)
    ledger[1]["repeat_number"] = 1
    _rebind_live(evidence)

    with pytest.raises(SemanticAssuranceReadinessError, match="repeat execution numbering"):
        assess_semantic_assurance_readiness(evidence)


@pytest.mark.parametrize("call_count", [1199, 1201])
def test_rejects_nonexact_total_chat_call_count(tmp_path: Path, call_count: int) -> None:
    evidence = _evidence(tmp_path)
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    execution = live["execution"]
    assert isinstance(execution, dict)
    execution["chat_call_count"] = call_count
    _rebind_live(evidence)

    with pytest.raises(SemanticAssuranceReadinessError, match="chat call coverage is not exact"):
        assess_semantic_assurance_readiness(evidence)


def test_rejects_tampered_repeat_execution_digest(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    execution = live["execution"]
    assert isinstance(execution, dict)
    ledger = execution["repeat_executions"]
    assert isinstance(ledger, list)
    ledger[0]["execution_digest"] = "0" * 64
    _refresh_live_digest(live)

    with pytest.raises(SemanticAssuranceReadinessError, match="repeat execution digest"):
        assess_semantic_assurance_readiness(evidence)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_id", "another/local-model"),
        ("model_variant", "qwen/qwen3.5-9b@q4_k"),
        ("endpoint", "http://localhost:13666"),
        ("endpoint", "http://127.0.0.1:13667"),
    ],
)
def test_rejects_nonfrozen_model_identity(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    evidence = _evidence(tmp_path)
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    model = live["model"]
    reports = live["offline_reports"]
    assert isinstance(model, dict) and isinstance(reports, list)
    model[field] = value
    for report in reports:
        assert isinstance(report, dict)
        invocation = report["invocation"]
        assert isinstance(invocation, dict)
        invocation[field] = value
    _rebind_live(evidence)

    with pytest.raises(SemanticAssuranceReadinessError, match="frozen local model"):
        assess_semantic_assurance_readiness(evidence)


def test_rejects_results_with_recomputed_digests_but_forged_aggregates(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    reports = live["offline_reports"]
    assert isinstance(reports, list)
    report = reports[0]
    assert isinstance(report, dict)
    results = report["results"]
    assert isinstance(results, list)
    results[0]["prediction"]["factuality"] = "false"
    derived = recompute_semantic_assurance_results(results)
    report["prediction_digest"] = derived["prediction_digest"]
    report["results_digest"] = derived["results_digest"]
    _rebind_live(evidence)

    with pytest.raises(SemanticAssuranceReadinessError, match="offline aggregates do not match canonical results"):
        assess_semantic_assurance_readiness(evidence)


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
    for report in reports:
        assert isinstance(report, dict)
        report["report_digest"] = _digest({key: value for key, value in report.items() if key != "report_digest"})
    execution = live["execution"]
    assert isinstance(execution, dict)
    ledger = execution["repeat_executions"]
    assert isinstance(ledger, list)
    for entry, report in zip(ledger, reports, strict=False):
        assert isinstance(entry, dict) and isinstance(report, dict)
        entry["prediction_digest"] = report["prediction_digest"]
        entry["execution_digest"] = _digest({key: value for key, value in entry.items() if key != "execution_digest"})
    _refresh_live_digest(live)


def _rebind_admission_in_live(evidence: dict[str, object]) -> None:
    admission = evidence["admission_receipt"]
    live = evidence["live_receipt"]
    assert isinstance(admission, dict) and isinstance(live, dict)
    reports = live["offline_reports"]
    assert isinstance(reports, list)
    for report in reports:
        assert isinstance(report, dict)
        dataset = report["dataset"]
        assert isinstance(dataset, dict)
        dataset["human_label_admission_digest"] = admission["admission_digest"]
    _rebind_live(evidence)


def _refresh_live_digest(live: dict[str, object]) -> None:
    live["report_digest"] = _digest({key: value for key, value in live.items() if key != "report_digest"})


def _rebind_dependents(evidence: dict[str, object]) -> None:
    admission = evidence["admission_receipt"]
    live = evidence["live_receipt"]
    policy = evidence["threshold_policy"]
    assert isinstance(admission, dict) and isinstance(live, dict) and isinstance(policy, dict)
    evaluated_run_digest = _evaluated_run_digest(live, admission)
    model_run_identity_digest = _digest({"model": dict(sorted(live["model"].items())), "prompt_contract": live["prompt_contract"], "partition": "sealed_holdout", "repeats": live["execution"]["repeats_completed"], "chat_call_count": live["execution"]["chat_call_count"]})
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


def _evaluated_run_digest(live: dict[str, object], admission: dict[str, object]) -> str:
    reports = live["offline_reports"]
    assert isinstance(reports, list) and reports
    return _digest({
        "live_report_digest": live["report_digest"],
        "report_digests": [report["report_digest"] for report in reports],
        "prediction_digest": reports[0]["prediction_digest"],
        "results_digest": reports[0]["results_digest"],
        "admission_digest": admission["admission_digest"],
    })
