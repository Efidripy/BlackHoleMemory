from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from blackholememory.semantic_assurance_corpus_admission import SemanticAssuranceCorpusAdmissionError
from blackholememory.semantic_assurance_corpus_admission import admit_semantic_assurance_corpus_for_human_review
from blackholememory.semantic_assurance_corpus_admission import verify_semantic_assurance_corpus_admission


RISKS = (
    "factuality", "entailment", "relevance", "direct_conflict", "temporal_conflict",
    "scope_conflict", "implicit_conflict", "injection", "cross_project",
    "retrieval_pressure", "abstention",
)
CONFLICTS = ("direct_conflict", "temporal_conflict", "scope_conflict", "implicit_conflict")


def _opaque(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _case(index: int, partition: str, risk: str) -> dict[str, object]:
    case_id = _opaque(f"case:{index}")
    return {
        "case_id": case_id,
        "partition": partition,
        "risk_class": risk,
        "isolation": {
            "family_id": _opaque(f"family:{partition}:{index}"),
            "group_id": _opaque(f"group:{partition}:{index}"),
            "source_group_id": _opaque(f"source-group:{partition}:{index}"),
            "source_theme": _opaque(f"theme:{partition}:{index}"),
            "prompt_template_id": _opaque(f"template:{partition}:{index}"),
            "paraphrase_cluster_id": _opaque(f"paraphrase:{partition}:{index}"),
        },
        "prompt": f"Assess proposition {_opaque(f'prompt:{index}')}",
        "candidate": f"Candidate statement {_opaque(f'candidate:{index}')}",
        "supports": [
            {"support_id": _opaque(f"support:{index}:a"), "evidence": f"Evidence alpha {_opaque(f'evidence:{index}:a')}"},
            {"support_id": _opaque(f"support:{index}:b"), "evidence": f"Evidence beta {_opaque(f'evidence:{index}:b')}"},
        ],
    }


def _raw() -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for partition, count in (("development", 80), ("validation", 80)):
        for index in range(count):
            cases.append(_case(len(cases), partition, RISKS[index % len(RISKS)]))
    for risk in CONFLICTS:
        for _ in range(50):
            cases.append(_case(len(cases), "sealed_holdout", risk))
    for index in range(200):
        cases.append(_case(len(cases), "sealed_holdout", RISKS[index % 3]))

    packet_cases = [
        {key: case[key] for key in ("case_id", "prompt", "candidate", "supports")}
        for case in cases
    ]
    second_packet = packet_cases[1:] + packet_cases[:1]

    def ledger(rows: list[dict[str, object]]) -> dict[str, object]:
        return {"rows": [
            {"case_id": row["case_id"], "support_ids": [item["support_id"] for item in row["supports"]], "labels": None}
            for row in rows
        ]}

    curator_ledger = {"rows": [
        {
            "case_id": case["case_id"],
            "semantic_id": _opaque(f"semantic:{case['case_id']}"),
            "proposition_id": _opaque(f"proposition:{case['case_id']}"),
            "mechanism_id": _opaque(f"mechanism:{case['case_id']}"),
            "source_fragments": [_opaque(f"fragment:{case['case_id']}")],
        }
        for case in cases
    ]}
    corpus = {"cases": cases}
    return {
        "schema_version": "bhm.semantic-assurance-corpus-admission.v1",
        "corpus": corpus,
        "packets": {"reviewer_a": {"cases": packet_cases}, "reviewer_b": {"cases": second_packet}},
        "ledgers": {"reviewer_a": ledger(packet_cases), "reviewer_b": ledger(second_packet)},
        "curator_attestation": {
            "claimed": True,
            "curator_identity_sha256": _opaque("curator:private-identity"),
            "corpus_digest": _opaque(json.dumps({"corpus": corpus, "curator_ledger": curator_ledger}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        },
        "curator_ledger": curator_ledger,
        "distribution": {"permitted": True},
    }


def _refresh_curator_binding(raw: dict[str, object]) -> None:
    raw["curator_attestation"]["corpus_digest"] = _opaque(json.dumps(
        {"corpus": raw["corpus"], "curator_ledger": raw["curator_ledger"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ))


def _v6_controls(cases: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "bhm.semantic-assurance-v6-curation-controls.v1",
        "case_controls": [
            {
                "case_id": case["case_id"],
                "atomic_fact_id": _opaque(f"atomic-fact:{case['case_id']}"),
                "support_count_bucket": "two",
                "approximate_length_bucket": "medium",
                "document_genre": "governance",
                "relation_form": "support",
                "adversarial_form": "none",
                "dimension_surfaces": ["factuality", "entailment", "relevance", "safe_operation"],
            }
            for case in cases
        ],
    }


def _rebuild_v6_packets_and_ledger(raw: dict[str, object]) -> None:
    corpus = raw["corpus"]
    assert isinstance(corpus, dict)
    cases = corpus["cases"]
    assert isinstance(cases, list)
    packet_cases = [
        {key: case[key] for key in ("case_id", "prompt", "candidate", "supports")}
        for case in cases
    ]
    second_packet = packet_cases[1:] + packet_cases[:1]

    def ledger(rows: list[dict[str, object]]) -> dict[str, object]:
        return {"rows": [
            {"case_id": row["case_id"], "support_ids": [item["support_id"] for item in row["supports"]], "labels": None}
            for row in rows
        ]}

    raw["packets"] = {"reviewer_a": {"cases": packet_cases}, "reviewer_b": {"cases": second_packet}}
    raw["ledgers"] = {"reviewer_a": ledger(packet_cases), "reviewer_b": ledger(second_packet)}
    raw["curator_ledger"] = {"rows": [
        {
            "case_id": case["case_id"],
            "semantic_id": _opaque(f"semantic:{case['case_id']}"),
            "proposition_id": _opaque(f"proposition:{case['case_id']}"),
            "mechanism_id": _opaque(f"mechanism:{case['case_id']}"),
            "source_fragments": [_opaque(f"fragment:{case['case_id']}")],
        }
        for case in cases
    ]}
    raw["v6_curation_controls"] = _v6_controls(cases)
    _refresh_curator_binding(raw)


def _raw_v6() -> dict[str, object]:
    raw = _raw()
    raw["schema_version"] = "bhm.semantic-assurance-corpus-admission.v2"
    raw["admission_profile"] = "v6_independent_human_curation"
    corpus = raw["corpus"]
    assert isinstance(corpus, dict)
    cases = corpus["cases"]
    assert isinstance(cases, list)
    cases[:] = [case for case in cases if case["partition"] != "sealed_holdout"]
    for partition in ("development", "validation"):
        for index in range(140):
            cases.append(_case(len(cases), partition, RISKS[index % len(RISKS)]))
    for risk in CONFLICTS:
        for _ in range(50):
            cases.append(_case(len(cases), "sealed_holdout", risk))
    for index in range(200):
        cases.append(_case(len(cases), "sealed_holdout", RISKS[index % len(RISKS)]))
    _rebuild_v6_packets_and_ledger(raw)
    return raw


def _digest_json(value: object) -> str:
    return _opaque(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _raw_v3() -> dict[str, object]:
    """Structural-only synthetic fixture; never human or source evidence."""

    raw = _raw_v6()
    raw["schema_version"] = "bhm.semantic-assurance-corpus-admission.v3"
    raw["admission_profile"] = "v6_curator_audited_independent_human_curation"
    cases = {case["case_id"]: case for case in raw["corpus"]["cases"]}
    bindings = []
    for control in raw["v6_curation_controls"]["case_controls"]:
        bindings.append({
            **control,
            "canonical_fragment_sha256": _opaque(f"canonical-fragment:{control['atomic_fact_id']}"),
            "source_kind": "public_docs",
        })
    raw["curator_case_bindings"] = {
        "schema_version": "bhm.semantic-assurance-v6-curator-case-bindings.v1",
        "rows": bindings,
    }
    fact_rows = sorted((
        {"atomic_fact_id": row["atomic_fact_id"], "partition": cases[row["case_id"]]["partition"]}
        for row in bindings
    ), key=lambda row: row["atomic_fact_id"])
    control_rows = sorted((
        {
            "atomic_fact_id": row["atomic_fact_id"],
            "risk_class": cases[row["case_id"]]["risk_class"],
            "support_count_bucket": row["support_count_bucket"],
            "approximate_length_bucket": row["approximate_length_bucket"],
            "document_genre": row["document_genre"],
            "relation_form": row["relation_form"],
            "adversarial_form": row["adversarial_form"],
            "dimension_surfaces": sorted(row["dimension_surfaces"]),
        }
        for row in bindings
    ), key=lambda row: row["atomic_fact_id"])
    fragment_rows = sorted((
        {
            "atomic_fact_id": row["atomic_fact_id"],
            "canonical_fragment_sha256": row["canonical_fragment_sha256"],
            "source_kind": row["source_kind"],
        }
        for row in bindings
    ), key=lambda row: row["atomic_fact_id"])
    partition_counts = {
        partition: sum(case["partition"] == partition for case in cases.values())
        for partition in ("development", "sealed_holdout", "validation")
    }
    partition_risk_counts = {
        partition: {risk: sum(case["partition"] == partition and case["risk_class"] == risk for case in cases.values()) for risk in RISKS}
        for partition in ("development", "sealed_holdout", "validation")
    }
    audit = {
        "atomic_fact_count": len(bindings),
        "partition_counts": partition_counts,
        "partition_risk_counts": partition_risk_counts,
        "sealed_conflict_counts": {risk: partition_risk_counts["sealed_holdout"][risk] for risk in CONFLICTS},
        "atomic_fact_binding_digest": _digest_json(fact_rows),
        "control_binding_digest": _digest_json(control_rows),
        "canonical_fragment_binding_digest": _digest_json(fragment_rows),
        "source_kind_counts": {"local_adr": 0, "public_docs": len(bindings)},
    }
    assurance = {
        "canonical_fragments_revalidated": True,
        "source_fragment_provenance_proven": False,
        "curator_identity_authenticated": False,
        "semantic_independence_proven": False,
        "semantic_truth_proven": False,
        "reviewer_cases_constructed": False,
        "automatic_apply_ready": False,
        "decision": "no_go",
    }
    execution = {
        "network_used": False,
        "model_called": False,
        "sqlite_mutation": False,
        "mem0_mutation": False,
        "qdrant_mutation": False,
        "automatic_apply": False,
    }
    audit_core = {
        "schema_version": "bhm.semantic-assurance-curator-audit-receipt.v1",
        "ok": True,
        "audit": audit,
        "assurance": assurance,
        "execution": execution,
    }
    raw["curator_audit"] = {**audit_core, "audit_digest": _digest_json(audit_core)}
    return raw


def _reduce_sealed_direct_conflicts_to_49(raw: dict[str, object]) -> None:
    corpus = raw["corpus"]
    assert isinstance(corpus, dict)
    cases = corpus["cases"]
    assert isinstance(cases, list)
    direct = [case for case in cases if case["partition"] == "sealed_holdout" and case["risk_class"] == "direct_conflict"]
    for case in direct[49:]:
        case["risk_class"] = "factuality"
    _refresh_curator_binding(raw)


def test_admits_complete_blinded_private_corpus_and_returns_content_free_no_go_receipt() -> None:
    raw = _raw()
    receipt = admit_semantic_assurance_corpus_for_human_review(raw)

    assert verify_semantic_assurance_corpus_admission(receipt) == receipt
    assert receipt["corpus"]["partition_counts"] == {"development": 80, "sealed_holdout": 400, "validation": 80}
    assert len(receipt["corpus"]["dataset_binding_digest"]) == 64
    assert set(receipt["corpus"]["risk_counts"]) == set(RISKS)
    assert set(receipt["corpus"]["sealed_risk_counts"]) == set(RISKS)
    assert sum(receipt["corpus"]["risk_counts"].values()) == 560
    assert sum(receipt["corpus"]["sealed_risk_counts"].values()) == 400
    assert receipt["assurance"] == {
        "status": "structurally_admitted_for_human_review_not_semantic_independence_proven",
        "curator_claimed_attestation": True,
        "curator_attestation_bound_to_corpus": True,
        "curator_identity_externally_authenticated": False,
        "semantic_independence_proven": False,
        "semantic_truth_proven": False,
        "automatic_apply_ready": False,
        "automatic_apply_decision": "no_go",
    }
    rendered = json.dumps(receipt, sort_keys=True)
    assert "Candidate statement" not in rendered
    assert "Evidence alpha" not in rendered


def test_v6_admits_exact_840_case_floor_all_partition_risk_cells_and_private_controls() -> None:
    receipt = admit_semantic_assurance_corpus_for_human_review(_raw_v6())

    assert receipt["schema_version"] == "bhm.semantic-assurance-corpus-admission-receipt.v2"
    assert receipt["admission_profile"] == "v6_independent_human_curation"
    assert receipt["corpus"]["partition_counts"] == {"development": 220, "sealed_holdout": 400, "validation": 220}
    assert receipt["corpus"]["case_count"] == 840
    assert all(
        set(counts) == set(RISKS) and all(value >= 1 for value in counts.values())
        for counts in receipt["corpus"]["partition_risk_counts"].values()
    )
    assert len(receipt["corpus"]["v6_control_binding_digest"]) == 64
    assert receipt["assurance"]["semantic_independence_proven"] is False
    assert receipt["assurance"]["automatic_apply_ready"] is False
    assert verify_semantic_assurance_corpus_admission(receipt) == receipt
    rendered = json.dumps(receipt, sort_keys=True)
    assert "atomic-fact" not in rendered
    assert "governance" not in rendered


def test_v3_exactly_joins_verified_curator_audit_without_exposing_private_evidence() -> None:
    receipt = admit_semantic_assurance_corpus_for_human_review(_raw_v3())

    assert receipt["schema_version"] == "bhm.semantic-assurance-corpus-admission-receipt.v3"
    assert receipt["admission_profile"] == "v6_curator_audited_independent_human_curation"
    assert receipt["assurance"]["curator_audit_exactly_joined"] is True
    assert set(receipt["curator_audit"]) == {
        "receipt_schema_version", "audit_digest", "atomic_fact_binding_digest",
        "control_binding_digest", "canonical_fragment_binding_digest",
    }
    assert verify_semantic_assurance_corpus_admission(receipt) == receipt
    rendered = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert "canonical-fragment" not in rendered
    assert "governance" not in rendered


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda raw: raw["curator_case_bindings"]["rows"][0].__setitem__("canonical_fragment_sha256", "0" * 64),
            "does not exactly bind",
        ),
        (
            lambda raw: raw["curator_case_bindings"]["rows"][0].__setitem__("source_kind", "local_adr"),
            "does not exactly bind",
        ),
        (
            lambda raw: raw["curator_case_bindings"]["rows"][0].__setitem__("relation_form", "absence"),
            "does not exactly match",
        ),
        (
            lambda raw: raw["curator_audit"]["audit"].__setitem__("canonical_fragment_binding_digest", "0" * 64),
            "invalid",
        ),
        (
            lambda raw: raw["curator_case_bindings"]["rows"].pop(),
            "cover every",
        ),
    ],
)
def test_v3_rejects_transplanted_or_mismatched_curator_audit_bindings(mutate, match: str) -> None:
    raw = _raw_v3()
    mutate(raw)
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match=match):
        admit_semantic_assurance_corpus_for_human_review(raw)


def test_v3_receipt_rejects_tampered_compact_curator_audit_binding() -> None:
    receipt = admit_semantic_assurance_corpus_for_human_review(_raw_v3())
    receipt["curator_audit"]["audit_digest"] = "0" * 64
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match="digest mismatch"):
        verify_semantic_assurance_corpus_admission(receipt)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda raw: (
                raw["corpus"]["cases"].pop(next(index for index, case in enumerate(raw["corpus"]["cases"]) if case["partition"] == "development")),
                _rebuild_v6_packets_and_ledger(raw),
            ),
            "partition coverage",
        ),
        (
            lambda raw: (
                [case.__setitem__("risk_class", "factuality") for case in raw["corpus"]["cases"] if case["partition"] == "validation" and case["risk_class"] == "abstention"],
                _refresh_curator_binding(raw),
            ),
            "risk coverage",
        ),
        (
            _reduce_sealed_direct_conflicts_to_49,
            "conflict bucket",
        ),
        (
            lambda raw: raw["v6_curation_controls"]["case_controls"][1].__setitem__("atomic_fact_id", raw["v6_curation_controls"]["case_controls"][0]["atomic_fact_id"]),
            "one-to-one",
        ),
        (
            lambda raw: raw["v6_curation_controls"]["case_controls"].pop(),
            "cover every",
        ),
    ],
)
def test_v6_rejects_weak_partition_risk_conflict_and_control_evidence(mutate, match: str) -> None:
    raw = _raw_v6()
    mutate(raw)
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match=match):
        admit_semantic_assurance_corpus_for_human_review(raw)


def test_v1_receipt_cannot_masquerade_as_v6_and_v6_receipt_is_tamper_evident() -> None:
    v1 = admit_semantic_assurance_corpus_for_human_review(_raw())
    v1["schema_version"] = "bhm.semantic-assurance-corpus-admission-receipt.v2"
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match="schema"):
        verify_semantic_assurance_corpus_admission(v1)

    v6 = admit_semantic_assurance_corpus_for_human_review(_raw_v6())
    v6["corpus"]["partition_risk_counts"]["development"]["factuality"] += 1
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match="partition risk"):
        verify_semantic_assurance_corpus_admission(v6)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda raw: raw["curator_attestation"].__setitem__("claimed", False), "attestation"),
        (lambda raw: raw["curator_attestation"].__setitem__("corpus_digest", _opaque("wrong")), "exact corpus"),
        (lambda raw: raw.__setitem__("distribution", {"permitted": False}), "distribution"),
        (lambda raw: raw["packets"]["reviewer_a"]["cases"][0].__setitem__("partition", "sealed_holdout"), "leaks"),
        (lambda raw: raw["ledgers"]["reviewer_a"]["rows"][0].__setitem__("labels", {}), "blank"),
        (lambda raw: raw["packets"]["reviewer_a"]["cases"][0].__setitem__("prompt", "See .docs/adr/0001.md:4"), "bind"),
        (lambda raw: raw["packets"]["reviewer_b"].__setitem__("cases", raw["packets"]["reviewer_a"]["cases"]), "positions"),
    ],
)
def test_rejects_attestation_packet_and_blank_ledger_failures(mutate, match: str) -> None:
    raw = _raw()
    mutate(raw)
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match=match):
        admit_semantic_assurance_corpus_for_human_review(raw)


def test_rejects_locator_split_disclosure_coverage_and_cross_split_text_collision() -> None:
    raw = _raw()
    raw["packets"]["reviewer_a"]["cases"][0]["prompt"] = "Review anchor_id=123"
    raw["packets"]["reviewer_b"]["cases"][-1]["prompt"] = "Review anchor_id=123"
    raw["corpus"]["cases"][0]["prompt"] = "Review anchor_id=123"
    _refresh_curator_binding(raw)
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match="locator"):
        admit_semantic_assurance_corpus_for_human_review(raw)

    raw = _raw()
    raw["corpus"]["cases"][0]["case_id"] = "development-visible-id"
    _refresh_curator_binding(raw)
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match="opaque"):
        admit_semantic_assurance_corpus_for_human_review(raw)

    raw = _raw()
    raw["corpus"]["cases"] = raw["corpus"]["cases"][:-1]
    _refresh_curator_binding(raw)
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match="coverage"):
        admit_semantic_assurance_corpus_for_human_review(raw)

    raw = _raw()
    first = raw["corpus"]["cases"][0]
    other = raw["corpus"]["cases"][80]
    other["prompt"] = first["prompt"].upper().replace(" ", "   ")
    raw["packets"]["reviewer_a"]["cases"][80]["prompt"] = other["prompt"]
    raw["packets"]["reviewer_b"]["cases"][79]["prompt"] = other["prompt"]
    _refresh_curator_binding(raw)
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match="collides"):
        admit_semantic_assurance_corpus_for_human_review(raw)


def test_rejects_reused_curator_fragment_and_cross_split_semantic_identity() -> None:
    raw = _raw()
    rows = raw["curator_ledger"]["rows"]
    rows[1]["source_fragments"] = rows[0]["source_fragments"]
    _refresh_curator_binding(raw)
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match="fragment"):
        admit_semantic_assurance_corpus_for_human_review(raw)

    raw = _raw()
    rows = raw["curator_ledger"]["rows"]
    rows[0]["source_fragments"] = ["not-a-digest"]
    _refresh_curator_binding(raw)
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match="SHA-256"):
        admit_semantic_assurance_corpus_for_human_review(raw)

    raw = _raw()
    rows = raw["curator_ledger"]["rows"]
    rows[80]["semantic_id"] = rows[0]["semantic_id"]
    _refresh_curator_binding(raw)
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match="crosses"):
        admit_semantic_assurance_corpus_for_human_review(raw)


def test_receipt_is_tamper_evident() -> None:
    receipt = admit_semantic_assurance_corpus_for_human_review(_raw())
    tampered = copy.deepcopy(receipt)
    tampered["assurance"]["automatic_apply_ready"] = True
    with pytest.raises(SemanticAssuranceCorpusAdmissionError, match="safety boundary"):
        verify_semantic_assurance_corpus_admission(tampered)


@pytest.mark.parametrize("mutate", [
    lambda receipt: receipt["corpus"].pop("dataset_binding_digest"),
    lambda receipt: receipt["corpus"].pop("sealed_case_binding_digest"),
    lambda receipt: receipt["corpus"].pop("sealed_support_binding_digest"),
    lambda receipt: receipt["corpus"].pop("risk_counts"),
    lambda receipt: receipt["corpus"].__setitem__("sealed_case_binding_digest", "0" * 64),
    lambda receipt: receipt["corpus"]["risk_counts"].__setitem__("factuality", 999),
    lambda receipt: receipt["corpus"]["sealed_risk_counts"].__setitem__("factuality", -1),
    lambda receipt: receipt["corpus"]["sealed_risk_counts"].__setitem__("unexpected", 0),
])
def test_receipt_requires_strict_dataset_binding_and_risk_counts(mutate) -> None:
    receipt = copy.deepcopy(admit_semantic_assurance_corpus_for_human_review(_raw()))
    mutate(receipt)
    with pytest.raises(SemanticAssuranceCorpusAdmissionError):
        verify_semantic_assurance_corpus_admission(receipt)


def test_local_cli_emits_only_content_free_receipt(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script_path = Path(__file__).parents[2] / "scripts" / "validate-bhm-semantic-assurance-corpus.py"
    spec = importlib.util.spec_from_file_location("semantic_assurance_corpus_cli", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    input_path = tmp_path / "private-corpus.json"
    output_path = tmp_path / "receipt.json"
    input_path.write_text(json.dumps(_raw()), encoding="utf-8")

    assert module.main(["--input", str(input_path), "--output", str(output_path)]) == 0

    rendered = capsys.readouterr().out
    assert json.loads(rendered) == json.loads(output_path.read_text(encoding="utf-8"))
    assert "Candidate statement" not in rendered
    assert "Evidence alpha" not in rendered
