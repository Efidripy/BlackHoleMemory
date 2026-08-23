from __future__ import annotations

import json
from pathlib import Path

from blackholememory.ontology_registry import OntologySchema


REPO_ROOT = Path(__file__).parents[2]


def _load_schema(name: str) -> OntologySchema:
    path = REPO_ROOT / "config" / "ontology" / name
    return OntologySchema.model_validate(json.loads(path.read_text(encoding="utf-8")))


def test_checked_in_blackholememory_link_ontology_is_declared_and_deterministic() -> None:
    schema = _load_schema("blackholememory-memory-links.v1.json")

    assert schema.project == "blackholememory"
    assert schema.activation_status == "declared"
    assert {item.name for item in schema.relation_types} == {
        "conflicts_with",
        "depends_on",
        "duplicate_of",
        "relates_to",
        "supersedes",
        "supports",
    }
    assert len(schema.digest()) == 64


def test_checked_in_multiserversubgen_ontology_is_narrow_and_evidence_bound() -> None:
    schema = _load_schema("multiserversubgen-memory-links.v1.json")

    assert schema.project == "multiserversubgen"
    assert schema.activation_status == "declared"
    assert {item.name for item in schema.entity_types} == {"memory"}
    assert {item.name for item in schema.relation_types} == {"depends_on"}
    assert schema.provenance["review"] == "WL-300.3"
    assert schema.provenance["eligible_depends_on_count"] == "106"
    assert len(schema.provenance["legacy_graph_digest"]) == 64
    assert len(schema.digest()) == 64
