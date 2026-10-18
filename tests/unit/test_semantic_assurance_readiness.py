from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from blackholememory.semantic_assurance_evaluation import evaluate_semantic_assurance
from blackholememory.semantic_assurance_evaluation import recompute_semantic_assurance_results
from blackholememory.semantic_assurance_corpus_admission import verify_semantic_assurance_corpus_admission
from blackholememory.semantic_assurance_label_admission import admit_semantic_assurance_human_labels
from blackholememory.semantic_assurance_readiness import SemanticAssuranceReadinessError
from blackholememory.semantic_assurance_readiness import assess_semantic_assurance_readiness
from blackholememory.semantic_assurance_readiness import assess_semantic_assurance_readiness_v3
from blackholememory.semantic_assurance_readiness import assess_semantic_assurance_readiness_v4
from blackholememory.semantic_assurance_readiness import load_semantic_assurance_readiness
from blackholememory.semantic_assurance_runner_attestation import runner_attestation_payload_digest
from blackholememory.semantic_assurance_runner_attestation import runner_trust_registry_digest
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


def _structural_unit_dataset(*, v6: bool = False) -> dict[str, object]:
    """Build only a synthetic structural fixture, never human/model evidence."""

    cases: list[dict[str, object]] = []
    sealed_risks = (
        [risk for risk in ("direct_conflict", "temporal_conflict", "scope_conflict", "implicit_conflict") for _ in range(50)]
        + [ALL_RISKS[index % len(ALL_RISKS)] for index in range(200)]
        if v6
        else [risk for risk in SEALED_RISKS for _ in range(50)]
    )
    sequence = 0
    for partition, count in (("development", 220 if v6 else 80), ("validation", 220 if v6 else 80), ("sealed_holdout", 400)):
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
                "expected": _expected_labels(partition, offset, support_ids),
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


def _expected_labels(partition: str, offset: int, support_ids: tuple[str, ...]) -> dict[str, object]:
    """Provide exact overlapping sealed strata for structural gate tests."""

    factuality = "true"
    entailment = "supported"
    conflict = "none"
    relevance = "relevant"
    if partition == "sealed_holdout":
        if offset < 80:
            factuality, entailment, conflict, relevance = "false", "contradicted", "direct", "irrelevant"
        elif offset < 160:
            factuality, entailment, conflict = "ambiguous", "insufficient", "temporal"
        elif offset < 240:
            factuality, conflict = "insufficient", "scope"
        elif offset < 320:
            conflict = "implicit"
    return {
        "factuality": factuality,
        "entailment": entailment,
        "conflict": conflict,
        "operation": "no_op",
        "support_relevance": {support_id: relevance for support_id in support_ids},
    }


def _policy() -> dict[str, object]:
    return {
        "schema_version": "bhm.semantic-assurance-threshold-policy.v2",
        "policy_id": "unit-policy-v2",
        "minimum_case_count": 400,
        "minimum_support_row_count": 800,
        "minimum_repeats": 3,
        "minimum_accuracy_lower_95": {
            "factuality": 0.95,
            "entailment": 0.95,
            "support_relevance": 0.95,
        },
        "minimum_conflict_recall_lower_95": 0.95,
        "minimum_stratum_support": {
            "factuality": {"false": 80, "ambiguous": 80, "insufficient": 80},
            "entailment": {"contradicted": 80, "insufficient": 80},
            "support_relevance": {"irrelevant": 80},
            "conflict": {"direct": 80, "temporal": 80, "scope": 80, "implicit": 80},
        },
        "minimum_stratum_exact_label_recall_lower_95": {
            "factuality": {"false": 0.95, "ambiguous": 0.95, "insufficient": 0.95},
            "entailment": {"contradicted": 0.95, "insufficient": 0.95},
            "support_relevance": {"irrelevant": 0.95},
            "conflict": {"direct": 0.95, "temporal": 0.95, "scope": 0.95, "implicit": 0.95},
        },
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
    v6: bool = False,
) -> dict[str, object]:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    labels = [{"case_id": case["case_id"], "labels": case["expected"]} for case in cases]
    manifest = {
        "schema_version": (
            "bhm.semantic-assurance-human-label-admission.v3" if v6
            else "bhm.semantic-assurance-human-label-admission.v2" if corpus_admitted
            else "bhm.semantic-assurance-human-label-admission.v1"
        ),
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
        manifest["corpus_admission"] = _corpus_admission_receipt(dataset, v6=v6)
    return admit_semantic_assurance_human_labels(manifest, dataset)


def _corpus_admission_receipt(dataset: dict[str, object], *, v6: bool = False) -> dict[str, object]:
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
    corpus: dict[str, object] = {
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
    }
    assurance: dict[str, object] = {
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
        corpus.update({
            "partition_risk_counts": {
                partition: {risk: sum(item["partition"] == partition and item["risk_class"] == risk for item in rows) for risk in sorted(ALL_RISKS)}
                for partition in ("development", "validation", "sealed_holdout")
            },
            "v6_control_binding_digest": _digest("synthetic-v6-control-binding"),
        })
        assurance.update({
            "status": "v6_structurally_admitted_for_human_review_not_semantic_independence_proven",
            "v6_partition_distribution_bound": True,
            "atomic_fact_case_one_to_one_declared": True,
        })
    core: dict[str, object] = {
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


def _evidence(tmp_path: Path, *, v6: bool = False) -> dict[str, object]:
    del tmp_path
    dataset = _structural_unit_dataset(v6=v6)
    policy = _policy()
    admission = _admission(dataset, policy, v6=v6)
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
        "schema_version": "bhm.semantic-assurance-readiness.v2",
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


def _source_identity() -> dict[str, str]:
    return {
        "schema_version": "bhm.semantic-assurance-source-identity.v1",
        "source_revision": "1" * 40,
        "source_tree_sha256": _digest("unit-source-tree"),
        "runner_manifest_digest": _digest("unit-runner-manifest"),
    }


def _event_chain(case_ids: list[str], before: object, after: object) -> dict[str, object]:
    previous = _digest({
        "domain": "bhm.semantic-assurance-live-event-chain.v3",
        "inventory_before_digest": _digest(before),
    })
    events: list[dict[str, object]] = []
    sequence = 0
    for repeat in (1, 2, 3):
        for case_id in case_ids:
            sequence += 1
            request_digest = _digest({"unit_request": repeat, "case_id": case_id})
            response_digest = _digest({"unit_response": repeat, "case_id": case_id})
            event = {
                "sequence": sequence,
                "repeat_number": repeat,
                "case_id": case_id,
                "request_digest": request_digest,
                "response_digest": response_digest,
                "event_digest": _digest({
                    "sequence": sequence,
                    "repeat_number": repeat,
                    "case_id": case_id,
                    "request_digest": request_digest,
                    "response_digest": response_digest,
                    "previous_event_digest": previous,
                }),
            }
            events.append(event)
            previous = str(event["event_digest"])
    root = _digest({
        "domain": "bhm.semantic-assurance-live-event-chain.v3",
        "inventory_before_digest": _digest(before),
        "inventory_after_digest": _digest(after),
        "event_count": len(events),
        "first_event_digest": events[0]["event_digest"],
        "last_event_digest": events[-1]["event_digest"],
    })
    return {
        "events": events,
        "event_count": len(events),
        "first_event_digest": events[0]["event_digest"],
        "last_event_digest": events[-1]["event_digest"],
        "chain_root": root,
        "inventory_before_digest": _digest(before),
        "inventory_after_digest": _digest(after),
    }


def _v3_live(evidence: dict[str, object]) -> None:
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    execution = live["execution"]
    inventory = live["inventory"]
    reports = live["offline_reports"]
    assert isinstance(execution, dict) and isinstance(inventory, dict) and isinstance(reports, list)
    first = reports[0]
    assert isinstance(first, dict)
    results = first["results"]
    assert isinstance(results, list)
    case_ids = sorted(str(row["case_id"]) for row in results)
    assert len(case_ids) == 400
    execution["event_chain"] = _event_chain(case_ids, inventory["before"], inventory["after"])
    live["schema_version"] = "bhm.semantic-assurance-live-receipt.v3"
    gate = live["gate"]
    assert isinstance(gate, dict)
    checks = gate["checks"]
    assert isinstance(checks, dict)
    checks["content_free_event_chain_integrity"] = True
    _refresh_live_digest(live)
    _rebind_dependents(evidence)


def _v4_live(evidence: dict[str, object]) -> None:
    _v3_live(evidence)
    admission = evidence["admission_receipt"]
    live = evidence["live_receipt"]
    assert isinstance(admission, dict) and isinstance(live, dict)
    corpus = admission["corpus"]
    corpus_admission = admission["corpus_admission"]
    assert isinstance(corpus, dict) and isinstance(corpus_admission, dict)
    live["schema_version"] = "bhm.semantic-assurance-live-receipt.v4"
    live["label_admission"] = {
        "receipt_schema_version": "bhm.semantic-assurance-human-label-admission-receipt.v3",
        "admission_digest": admission["admission_digest"],
        "corpus_pre_admission_digest": corpus["corpus_pre_admission_digest"],
        "corpus_admission_digest": corpus_admission["admission_digest"],
        "admission_profile": corpus_admission["admission_profile"],
        "partition_risk_counts": corpus_admission["partition_risk_counts"],
        "v6_control_binding_digest": corpus_admission["v6_control_binding_digest"],
    }
    gate = live["gate"]
    assert isinstance(gate, dict)
    checks = gate["checks"]
    assert isinstance(checks, dict)
    checks["v6_label_admission_bound"] = True
    _refresh_live_digest(live)
    _rebind_dependents(evidence)


def _runner_registry(private: Ed25519PrivateKey) -> dict[str, object]:
    public_key = private.public_key().public_bytes_raw()
    registry: dict[str, object] = {
        "schema_version": "bhm.semantic-assurance-runner-trust.v1",
        "registry_id": "readiness-v3-unit-registry",
        "signers": [{
            "signer_id": "readiness-v3-runner",
            "role": "trusted_runner",
            "public_key_sha256": hashlib.sha256(public_key).hexdigest(),
            "active": True,
        }],
        "registry_digest": "",
    }
    registry["registry_digest"] = runner_trust_registry_digest(registry)
    return registry


def _v3_pre_digest(evidence: dict[str, object], source: dict[str, str]) -> str:
    v2 = assess_semantic_assurance_readiness({
        "schema_version": "bhm.semantic-assurance-readiness.v2",
        **{key: evidence[key] for key in (
            "admission_receipt", "live_receipt", "threshold_policy", "risk_coverage",
            "shadow_human_oracle", "sqlite_rehearsal", "architecture_invariants", "owner_charter",
        )},
    }, _allow_live_v3=True)
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    execution = live["execution"]
    inventory = live["inventory"]
    assert isinstance(execution, dict) and isinstance(inventory, dict)
    chain = execution["event_chain"]
    assert isinstance(chain, dict)
    chain_summary = {key: chain[key] for key in (
        "event_count", "first_event_digest", "last_event_digest", "chain_root",
        "inventory_before_digest", "inventory_after_digest",
    )}
    core = {
        "schema_version": "bhm.semantic-assurance-pre-attestation-core.v1",
        "source_identity": source,
        "readiness_v2": {key: value for key, value in v2.items() if key != "readiness_digest"},
        "live_report_digest": live["report_digest"],
        "event_chain": chain_summary,
        "inventory_before_digest": _digest(inventory["before"]),
        "inventory_after_digest": _digest(inventory["after"]),
    }
    return _digest(core)


def _v4_pre_digest(evidence: dict[str, object], source: dict[str, str]) -> str:
    v2 = assess_semantic_assurance_readiness({
        "schema_version": "bhm.semantic-assurance-readiness.v2",
        **{key: evidence[key] for key in (
            "admission_receipt", "live_receipt", "threshold_policy", "risk_coverage",
            "shadow_human_oracle", "sqlite_rehearsal", "architecture_invariants", "owner_charter",
        )},
    }, _allow_live_v4=True, _required_label_admission_schema="bhm.semantic-assurance-human-label-admission-receipt.v3")
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    execution = live["execution"]
    inventory = live["inventory"]
    label_admission = live["label_admission"]
    assert isinstance(execution, dict) and isinstance(inventory, dict) and isinstance(label_admission, dict)
    chain = execution["event_chain"]
    assert isinstance(chain, dict)
    chain_summary = {key: chain[key] for key in (
        "event_count", "first_event_digest", "last_event_digest", "chain_root",
        "inventory_before_digest", "inventory_after_digest",
    )}
    core = {
        "schema_version": "bhm.semantic-assurance-pre-attestation-core.v2",
        "source_identity": source,
        "readiness_v2": {key: value for key, value in v2.items() if key != "readiness_digest"},
        "v6_label_admission": label_admission,
        "live_report_digest": live["report_digest"],
        "event_chain": chain_summary,
        "inventory_before_digest": _digest(inventory["before"]),
        "inventory_after_digest": _digest(inventory["after"]),
    }
    return _digest(core)


def _v3_evidence(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    evidence = _evidence(tmp_path)
    _v3_live(evidence)
    source = _source_identity()
    private = Ed25519PrivateKey.generate()
    registry = _runner_registry(private)
    admission = evidence["admission_receipt"]
    live = evidence["live_receipt"]
    policy = evidence["threshold_policy"]
    assert isinstance(admission, dict) and isinstance(live, dict) and isinstance(policy, dict)
    corpus_admission = admission["corpus_admission"]
    execution = live["execution"]
    inventory = live["inventory"]
    assert isinstance(corpus_admission, dict) and isinstance(execution, dict) and isinstance(inventory, dict)
    chain = execution["event_chain"]
    assert isinstance(chain, dict)
    public_key_b64 = base64.b64encode(private.public_key().public_bytes_raw()).decode("ascii")
    runner: dict[str, object] = {
        "schema_version": "bhm.semantic-assurance-runner-attestation.v2",
        **{key: value for key, value in source.items() if key != "schema_version"},
        "model": {"model_id": DEFAULT_MODEL_ID, "model_variant": DEFAULT_MODEL_VARIANT, "endpoint": DEFAULT_BASE_URL},
        "prompt_contract_digest": hashlib.sha256(PROMPT_CONTRACT.encode("utf-8")).hexdigest(),
        "corpus_admission_digest": corpus_admission["admission_digest"],
        "label_admission_digest": admission["admission_digest"],
        "threshold_policy_digest": _digest(policy),
        "live_report_digest": live["report_digest"],
        "pre_attestation_readiness_digest": _v3_pre_digest(evidence, source),
        "event_chain": {
            "event_count": chain["event_count"],
            "first_event_digest": chain["first_event_digest"],
            "last_event_digest": chain["last_event_digest"],
            "chain_root": chain["chain_root"],
        },
        "inventory_before_digest": _digest(inventory["before"]),
        "inventory_after_digest": _digest(inventory["after"]),
        "execution": {
            "read_only": True, "automatic_apply": False, "sqlite_mutation": False,
            "mem0_mutation": False, "qdrant_mutation": False, "model_load_requested": False,
            "model_unload_requested": False, "model_download_requested": False,
            "model_configuration_requested": False,
        },
        "signer": {"signer_id": "readiness-v3-runner", "public_key_b64": public_key_b64, "signature_b64": ""},
        "trust_registry_digest": registry["registry_digest"],
    }
    runner["signer"]["signature_b64"] = base64.b64encode(private.sign(bytes.fromhex(runner_attestation_payload_digest(runner)))).decode("ascii")  # type: ignore[index]
    evidence = {
        **evidence,
        "schema_version": "bhm.semantic-assurance-readiness.v3",
        "source_identity": source,
        "runner_attestation": runner,
    }
    return evidence, registry


def _v4_evidence(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    evidence = _evidence(tmp_path, v6=True)
    _v4_live(evidence)
    source = _source_identity()
    private = Ed25519PrivateKey.generate()
    registry = _runner_registry(private)
    admission = evidence["admission_receipt"]
    live = evidence["live_receipt"]
    policy = evidence["threshold_policy"]
    assert isinstance(admission, dict) and isinstance(live, dict) and isinstance(policy, dict)
    corpus_admission = admission["corpus_admission"]
    execution = live["execution"]
    inventory = live["inventory"]
    assert isinstance(corpus_admission, dict) and isinstance(execution, dict) and isinstance(inventory, dict)
    chain = execution["event_chain"]
    assert isinstance(chain, dict)
    public_key_b64 = base64.b64encode(private.public_key().public_bytes_raw()).decode("ascii")
    runner: dict[str, object] = {
        "schema_version": "bhm.semantic-assurance-runner-attestation.v2",
        **{key: value for key, value in source.items() if key != "schema_version"},
        "model": {"model_id": DEFAULT_MODEL_ID, "model_variant": DEFAULT_MODEL_VARIANT, "endpoint": DEFAULT_BASE_URL},
        "prompt_contract_digest": hashlib.sha256(PROMPT_CONTRACT.encode("utf-8")).hexdigest(),
        "corpus_admission_digest": corpus_admission["admission_digest"],
        "label_admission_digest": admission["admission_digest"],
        "threshold_policy_digest": _digest(policy),
        "live_report_digest": live["report_digest"],
        "pre_attestation_readiness_digest": _v4_pre_digest(evidence, source),
        "event_chain": {
            "event_count": chain["event_count"],
            "first_event_digest": chain["first_event_digest"],
            "last_event_digest": chain["last_event_digest"],
            "chain_root": chain["chain_root"],
        },
        "inventory_before_digest": _digest(inventory["before"]),
        "inventory_after_digest": _digest(inventory["after"]),
        "execution": {
            "read_only": True, "automatic_apply": False, "sqlite_mutation": False,
            "mem0_mutation": False, "qdrant_mutation": False, "model_load_requested": False,
            "model_unload_requested": False, "model_download_requested": False,
            "model_configuration_requested": False,
        },
        "signer": {"signer_id": "readiness-v3-runner", "public_key_b64": public_key_b64, "signature_b64": ""},
        "trust_registry_digest": registry["registry_digest"],
    }
    runner["signer"]["signature_b64"] = base64.b64encode(private.sign(bytes.fromhex(runner_attestation_payload_digest(runner)))).decode("ascii")  # type: ignore[index]
    evidence = {
        **evidence,
        "schema_version": "bhm.semantic-assurance-readiness.v4",
        "source_identity": source,
        "runner_attestation": runner,
    }
    return evidence, registry




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


def test_v3_validates_ephemeral_trusted_runner_without_hash_cycle(tmp_path: Path) -> None:
    evidence, registry = _v3_evidence(tmp_path)

    receipt = assess_semantic_assurance_readiness_v3(evidence, trust_registry=registry)

    assert receipt["schema_version"] == "bhm.semantic-assurance-readiness-receipt.v3"
    assert receipt["checks"]["trusted_runner_attestation"] is True
    assert receipt["checks"]["pre_attestation_no_cycle"] is True
    assert receipt["decision"]["automatic_apply_ready"] is False
    assert receipt["decision"]["decision"] == "no_go"
    assert receipt["bindings"]["pre_attestation_readiness_digest"] != receipt["readiness_digest"]


def test_v3_rejects_empty_repository_runner_registry(tmp_path: Path) -> None:
    evidence, _registry = _v3_evidence(tmp_path)

    with pytest.raises(SemanticAssuranceReadinessError, match="trusted runner attestation"):
        assess_semantic_assurance_readiness_v3(evidence)


def test_v3_rejects_wrong_signed_runner_binding(tmp_path: Path) -> None:
    evidence, registry = _v3_evidence(tmp_path)
    source = evidence["source_identity"]
    assert isinstance(source, dict)
    # Keep the signed envelope valid, but change the independently supplied
    # source binding.  This proves v3 reconstructs expected bindings instead
    # of accepting whichever digest the runner envelope declares.
    source["source_tree_sha256"] = "f" * 64

    with pytest.raises(SemanticAssuranceReadinessError, match="trusted runner attestation"):
        assess_semantic_assurance_readiness_v3(evidence, trust_registry=registry)


def test_v3_rejects_event_chain_substitution_even_with_recomputed_live_digest(tmp_path: Path) -> None:
    evidence, registry = _v3_evidence(tmp_path)
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    execution = live["execution"]
    assert isinstance(execution, dict)
    chain = execution["event_chain"]
    assert isinstance(chain, dict)
    events = chain["events"]
    assert isinstance(events, list)
    events[0]["case_id"] = "substituted-case-id"
    _refresh_live_digest(live)
    _rebind_dependents(evidence)

    with pytest.raises(SemanticAssuranceReadinessError, match="event chain"):
        assess_semantic_assurance_readiness_v3(evidence, trust_registry=registry)


def test_v4_preserves_v6_bindings_in_signed_readiness_path(tmp_path: Path) -> None:
    evidence, registry = _v4_evidence(tmp_path)

    receipt = assess_semantic_assurance_readiness_v4(evidence, trust_registry=registry)

    assert receipt["schema_version"] == "bhm.semantic-assurance-readiness-receipt.v4"
    assert receipt["bindings"]["admission_profile"] == "v6_independent_human_curation"
    assert set(receipt["bindings"]["partition_risk_counts"]) == {"development", "validation", "sealed_holdout"}
    assert len(receipt["bindings"]["v6_control_binding_digest"]) == 64
    assert receipt["checks"]["v6_label_admission_binding"] is True
    assert receipt["checks"]["v6_live_binding"] is True
    assert receipt["decision"]["automatic_apply_ready"] is False
    assert receipt["decision"]["decision"] == "no_go"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda evidence: evidence["admission_receipt"]["corpus_admission"].__setitem__("admission_profile", "ordinary_curation"),
            "human-label admission receipt",
        ),
        (
            lambda evidence: evidence["live_receipt"].__setitem__("schema_version", "bhm.semantic-assurance-live-receipt.v3"),
                "requires a V6 live receipt",
        ),
        (
            lambda evidence: evidence["live_receipt"]["label_admission"].__setitem__("v6_control_binding_digest", "f" * 64),
            "live V6 label binding",
        ),
    ],
)
def test_v4_rejects_tier_downgrade_and_substituted_v6_bindings(tmp_path: Path, mutate, match: str) -> None:
    evidence, registry = _v4_evidence(tmp_path)
    mutate(evidence)
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    _refresh_live_digest(live)

    with pytest.raises(SemanticAssuranceReadinessError, match=match):
        assess_semantic_assurance_readiness_v4(evidence, trust_registry=registry)


@pytest.mark.parametrize("field", ("admission_profile", "partition_risk_counts", "v6_control_binding_digest"))
def test_v4_rejects_rebound_v6_label_tampering(tmp_path: Path, field: str) -> None:
    evidence, registry = _v4_evidence(tmp_path)
    admission = evidence["admission_receipt"]
    assert isinstance(admission, dict)
    corpus_admission = admission["corpus_admission"]
    assert isinstance(corpus_admission, dict)
    if field == "admission_profile":
        corpus_admission[field] = "ordinary_curation"
    elif field == "partition_risk_counts":
        counts = corpus_admission[field]
        assert isinstance(counts, dict)
        counts["development"]["factuality"] += 1
        counts["development"]["entailment"] -= 1
    else:
        corpus_admission[field] = "f" * 64
    admission["admission_digest"] = _digest({key: value for key, value in admission.items() if key != "admission_digest"})
    _rebind_admission_in_live(evidence)
    _rebind_dependents(evidence)

    with pytest.raises(SemanticAssuranceReadinessError):
        assess_semantic_assurance_readiness_v4(evidence, trust_registry=registry)


@pytest.mark.parametrize("schema", (
    "bhm.semantic-assurance-human-label-admission-receipt.v1",
    "bhm.semantic-assurance-human-label-admission-receipt.v2",
))
def test_v4_rejects_legacy_label_receipts_before_measurement(tmp_path: Path, schema: str) -> None:
    evidence, registry = _v4_evidence(tmp_path)
    admission = evidence["admission_receipt"]
    assert isinstance(admission, dict)
    admission["schema_version"] = schema

    with pytest.raises(SemanticAssuranceReadinessError, match="exact declared human-label receipt version|human-label admission receipt"):
        assess_semantic_assurance_readiness_v4(evidence, trust_registry=registry)


def test_v4_rejects_legacy_live_receipt_before_measurement(tmp_path: Path) -> None:
    evidence, registry = _v4_evidence(tmp_path)
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    live["schema_version"] = "bhm.semantic-assurance-live-receipt.v1"

    with pytest.raises(SemanticAssuranceReadinessError, match="requires a V6 live receipt"):
        assess_semantic_assurance_readiness_v4(evidence, trust_registry=registry)


def test_v3_remains_closed_to_forward_only_v6_label_receipt(tmp_path: Path) -> None:
    evidence, registry = _v4_evidence(tmp_path)
    evidence["schema_version"] = "bhm.semantic-assurance-readiness.v3"

    with pytest.raises(SemanticAssuranceReadinessError, match="v2 corpus-admitted"):
        assess_semantic_assurance_readiness_v3(evidence, trust_registry=registry)


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
    results[0]["prediction"]["factuality"] = "true"
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


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "wrong",
    ],
)
def test_rejects_missing_or_wrong_stratified_policy_shape(
    tmp_path: Path,
    kind: str,
) -> None:
    evidence = _evidence(tmp_path)
    policy = evidence["threshold_policy"]
    assert isinstance(policy, dict)
    if kind == "missing":
        del policy["minimum_stratum_support"]["factuality"]["false"]
    else:
        policy["minimum_stratum_exact_label_recall_lower_95"]["conflict"]["unknown"] = 0.95

    with pytest.raises(SemanticAssuranceReadinessError, match="stratified minima are invalid"):
        assess_semantic_assurance_readiness(evidence)


def test_rejects_policy_that_weakens_required_stratified_sealed_minima(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    policy = evidence["threshold_policy"]
    assert isinstance(policy, dict)
    policy["minimum_stratum_support"]["factuality"]["false"] = 79

    with pytest.raises(SemanticAssuranceReadinessError, match="weakens stratified sealed minima"):
        assess_semantic_assurance_readiness(evidence)


def test_rejects_derived_stratum_undercoverage(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _change_all_repeat_results(
        evidence,
        lambda result: result["expected"].update({"factuality": "true"}) if result["expected"]["factuality"] == "false" else None,
        limit=1,
    )

    with pytest.raises(SemanticAssuranceReadinessError, match="stratified sample minimum"):
        assess_semantic_assurance_readiness(evidence)


def test_rejects_derived_stratum_misprediction(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    _change_all_repeat_results(
        evidence,
        lambda result: result["prediction"].update({"factuality": "true"}) if result["expected"]["factuality"] == "false" else None,
        limit=1,
    )

    with pytest.raises(SemanticAssuranceReadinessError, match="stratified lower confidence bound"):
        assess_semantic_assurance_readiness(evidence)


def _change_all_repeat_results(
    evidence: dict[str, object],
    change: object,
    *,
    limit: int,
) -> None:
    """Apply an equivalent canonical-row change to each deterministic repeat."""

    assert callable(change)
    live = evidence["live_receipt"]
    assert isinstance(live, dict)
    reports = live["offline_reports"]
    assert isinstance(reports, list)
    for report in reports:
        assert isinstance(report, dict)
        results = report["results"]
        assert isinstance(results, list)
        changed = 0
        for result in results:
            before = _digest(result)
            change(result)
            if _digest(result) != before:
                changed += 1
                if changed == limit:
                    break
        assert changed == limit
        _rebind_report_from_results(report)
    _rebind_live(evidence)
    _rebind_dependents(evidence)


def _rebind_report_from_results(report: dict[str, object]) -> None:
    results = report["results"]
    assert isinstance(results, list)
    derived = recompute_semantic_assurance_results(results)
    report["prediction_digest"] = derived["prediction_digest"]
    report["results_digest"] = derived["results_digest"]
    report["counts"] = {
        "case_count": derived["case_count"],
        "support_row_count": derived["support_row_count"],
        "exact_case_match_count": sum(row["exact_match"] for row in derived["results"]),
        "exact_case_mismatch_count": sum(not row["exact_match"] for row in derived["results"]),
    }
    report["metrics"] = derived["metrics"]
    report["safety"] = derived["safety"]


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
