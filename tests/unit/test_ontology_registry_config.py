from __future__ import annotations

import json
from pathlib import Path

from blackholememory.ontology_registry import OntologySchema


def test_checked_in_blackholememory_link_ontology_is_declared_and_deterministic() -> None:
    path = Path(__file__).parents[2] / "config" / "ontology" / "blackholememory-memory-links.v1.json"
    schema = OntologySchema.model_validate(json.loads(path.read_text(encoding="utf-8")))

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
