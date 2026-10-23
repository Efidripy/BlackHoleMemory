from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from blackholememory.semantic_assurance_curator_audit import (
    SemanticAssuranceCuratorAuditError,
    audit_v6_curator_inputs,
    verify_v6_curator_audit_receipt,
)

RISKS = (
    "factuality", "entailment", "relevance", "direct_conflict", "temporal_conflict",
    "scope_conflict", "implicit_conflict", "injection", "cross_project",
    "retrieval_pressure", "abstention",
)
CONFLICTS = ("direct_conflict", "temporal_conflict", "scope_conflict", "implicit_conflict")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "canonical"
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for index in range(840):
        lines.extend((f"## Curated fact {index}", f"Evidence line {index}"))
    (docs / "curator.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _risk(partition: str, offset: int) -> str:
    if partition == "sealed_holdout" and offset < 200:
        return CONFLICTS[offset // 50]
    return RISKS[offset % len(RISKS)]


def _inputs(tmp_path: Path) -> tuple[Path, dict[str, object], dict[str, object]]:
    root = _root(tmp_path)
    facts: list[dict[str, object]] = []
    controls: list[dict[str, object]] = []
    index = 0
    for partition, count in (("development", 220), ("validation", 220), ("sealed_holdout", 400)):
        for offset in range(count):
            fact_id = _digest(f"atomic:{index}")
            evidence = f"Evidence line {index}"
            facts.append({
                "atomic_fact_id": fact_id,
                "partition": partition,
                "canonical_path": "docs/curator.md",
                "canonical_heading": f"Curated fact {index}",
                "line_start": index * 2 + 2,
                "line_end": index * 2 + 2,
                "fragment_sha256": _digest(evidence),
                "proposition_private": f"Private proposition {partition} {index}",
                "inference_mechanism_private": f"Private mechanism {partition} {index}",
                "relation_form_private": "support",
                "source_verified_by_human": True,
            })
            controls.append({
                "atomic_fact_id": fact_id,
                "risk_class": _risk(partition, offset),
                "support_count_bucket": "one",
                "approximate_length_bucket": "short",
                "document_genre": "governance",
                "relation_form": "support",
                "adversarial_form": "none",
                "dimension_surfaces": ["factuality", "entailment", "relevance", "safe_operation"],
            })
            index += 1
    return root, {
        "schema_version": "bhm.semantic-assurance-human-curation-ledger.v1",
        "curator_identity_attested": True,
        "atomic_facts": facts,
    }, {
        "schema_version": "bhm.semantic-assurance-human-curation-controls.v1",
        "control_cells": controls,
    }


def _reuse_fragment(root: Path, facts: dict[str, object], controls: dict[str, object]) -> None:
    del root, controls
    first, second = facts["atomic_facts"][:2]
    for key in ("canonical_heading", "line_start", "line_end", "fragment_sha256"):
        second[key] = first[key]


def test_admits_complete_v6_private_curation_and_returns_content_free_no_go_receipt(tmp_path: Path) -> None:
    root, facts, controls = _inputs(tmp_path)
    receipt = audit_v6_curator_inputs(facts, controls, canonical_root=root)

    assert verify_v6_curator_audit_receipt(receipt) == receipt
    assert receipt["audit"]["atomic_fact_count"] == 840
    assert receipt["audit"]["partition_counts"] == {"development": 220, "sealed_holdout": 400, "validation": 220}
    assert all(value >= 50 for value in receipt["audit"]["sealed_conflict_counts"].values())
    assert receipt["assurance"]["automatic_apply_ready"] is False
    assert receipt["assurance"]["decision"] == "no_go"
    rendered = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert "Private proposition" not in rendered
    assert "Private mechanism" not in rendered
    assert "docs/curator.md" not in rendered
    assert "Evidence line" not in rendered


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda root, facts, controls: facts.__setitem__("curator_identity_attested", False), "incomplete"),
        (lambda root, facts, controls: facts["atomic_facts"][0].__setitem__("canonical_path", "../secret.md"), "escapes"),
        (lambda root, facts, controls: facts["atomic_facts"][0].__setitem__("canonical_path", "README.md"), "allowlist"),
        (lambda root, facts, controls: facts["atomic_facts"][0].__setitem__("canonical_heading", "Missing heading"), "heading"),
        (lambda root, facts, controls: facts["atomic_facts"][0].__setitem__("line_end", 9_999), "exceeds"),
        (lambda root, facts, controls: facts["atomic_facts"][0].__setitem__("fragment_sha256", "0" * 64), "does not match"),
        (_reuse_fragment, "reused"),
        (lambda root, facts, controls: controls["control_cells"].pop(), "cover every"),
        (lambda root, facts, controls: controls["control_cells"][0].__setitem__("risk_class", "unknown"), "closed-vocabulary"),
        (lambda root, facts, controls: facts["atomic_facts"][220].__setitem__("proposition_private", facts["atomic_facts"][0]["proposition_private"]), "reused"),
    ],
)
def test_rejects_bad_private_source_and_control_inputs(tmp_path: Path, mutate: object, match: str) -> None:
    root, facts, controls = _inputs(tmp_path)
    assert callable(mutate)
    mutate(root, facts, controls)
    with pytest.raises(SemanticAssuranceCuratorAuditError, match=match):
        audit_v6_curator_inputs(facts, controls, canonical_root=root)


def test_rejects_weak_distribution_and_one_to_many_controls(tmp_path: Path) -> None:
    root, facts, controls = _inputs(tmp_path)
    sealed_direct = [
        control for control in controls["control_cells"]
        if control["risk_class"] == "direct_conflict" and any(
            fact["atomic_fact_id"] == control["atomic_fact_id"] and fact["partition"] == "sealed_holdout"
            for fact in facts["atomic_facts"]
        )
    ]
    for control in sealed_direct[49:]:
        control["risk_class"] = "factuality"
    with pytest.raises(SemanticAssuranceCuratorAuditError, match="sealed curator conflict"):
        audit_v6_curator_inputs(facts, controls, canonical_root=root)

    root, facts, controls = _inputs(tmp_path)
    controls["control_cells"].append(copy.deepcopy(controls["control_cells"][0]))
    with pytest.raises(SemanticAssuranceCuratorAuditError, match="exactly once"):
        audit_v6_curator_inputs(facts, controls, canonical_root=root)


def test_fragment_hash_is_lf_normalized_and_local_adr_is_content_bound(tmp_path: Path) -> None:
    root, facts, controls = _inputs(tmp_path)
    adr = root / ".docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "0001.md").write_bytes(b"# ADR One\r\nCanonical ADR evidence\r\n")
    fact = facts["atomic_facts"][0]
    fact.update({
        "canonical_path": ".docs/adr/0001.md",
        "canonical_heading": "ADR One",
        "line_start": 2,
        "line_end": 2,
        "fragment_sha256": _digest("Canonical ADR evidence"),
    })
    receipt = audit_v6_curator_inputs(facts, controls, canonical_root=root)
    assert receipt["audit"]["source_kind_counts"] == {"local_adr": 1, "public_docs": 839}


def test_receipt_is_tamper_evident(tmp_path: Path) -> None:
    root, facts, controls = _inputs(tmp_path)
    receipt = audit_v6_curator_inputs(facts, controls, canonical_root=root)
    receipt["assurance"]["automatic_apply_ready"] = True
    with pytest.raises(SemanticAssuranceCuratorAuditError, match="safety boundary"):
        verify_v6_curator_audit_receipt(receipt)


def test_cli_writes_only_content_free_receipt(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, facts, controls = _inputs(tmp_path)
    atomic_path, controls_path, output_path = tmp_path / "atomic.json", tmp_path / "controls.json", tmp_path / "receipt.json"
    atomic_path.write_text(json.dumps(facts), encoding="utf-8")
    controls_path.write_text(json.dumps(controls), encoding="utf-8")
    script = Path(__file__).parents[2] / "scripts" / "validate-bhm-semantic-assurance-curator.py"
    spec = importlib.util.spec_from_file_location("semantic_assurance_curator_cli", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.main(["--atomic-facts", str(atomic_path), "--control-cells", str(controls_path), "--canonical-root", str(root), "--output", str(output_path)]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(output_path.read_text(encoding="utf-8"))
