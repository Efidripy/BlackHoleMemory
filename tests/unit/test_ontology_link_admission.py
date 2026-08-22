from __future__ import annotations

import pytest
from fastapi import HTTPException

from blackholememory import app as bhm_app
from blackholememory.ontology_registry import OntologyEntityType
from blackholememory.ontology_registry import OntologyRelationType
from blackholememory.ontology_registry import OntologySchema


def _schema() -> OntologySchema:
    return OntologySchema(
        project="blackholememory",
        owner="operator",
        activation_status="declared",
        entity_types=(OntologyEntityType(name="memory"),),
        relation_types=(
            OntologyRelationType(
                name="relates_to",
                source_types=("memory",),
                target_types=("memory",),
            ),
        ),
    )


def _memory(memory_id: str) -> dict[str, str]:
    return {"source_id": memory_id, "project": "blackholememory"}


def test_active_ontology_pins_admission_metadata_on_allowed_link(monkeypatch) -> None:
    schema = _schema()
    saved: dict[str, list[dict]] = {}

    monkeypatch.setattr(bhm_app, "_active_ontology_schema", lambda _project: schema)
    monkeypatch.setattr(bhm_app, "_find_live_memory", lambda memory_id, _project: _memory(memory_id))
    monkeypatch.setattr(bhm_app, "_load_memory_links", lambda: [])
    monkeypatch.setattr(bhm_app, "_save_memory_links", lambda links: saved.setdefault("links", links))

    link = bhm_app._create_memory_link(
        bhm_app.MemoryLinkRequest(
            source_id="mem-source",
            target_id="mem-target",
            relation="relates_to",
            project="blackholememory",
            ontology_schema_digest=schema.digest(),
        )
    )

    assert saved["links"] == [link]
    assert link["metadata"]["ontology"] == {
        "schema_digest": schema.digest(),
        "revision": 1,
        "relation": "relates_to",
        "source_type": "memory",
        "target_type": "memory",
        "admission": "allow",
    }


def test_active_ontology_quarantines_unknown_relation_without_storage_write(monkeypatch) -> None:
    schema = _schema()
    writes: list[list[dict]] = []

    monkeypatch.setattr(bhm_app, "_active_ontology_schema", lambda _project: schema)
    monkeypatch.setattr(bhm_app, "_find_live_memory", lambda memory_id, _project: _memory(memory_id))
    monkeypatch.setattr(bhm_app, "_load_memory_links", lambda: [])
    monkeypatch.setattr(bhm_app, "_save_memory_links", lambda links: writes.append(links))

    with pytest.raises(HTTPException) as exc_info:
        bhm_app._create_memory_link(
            bhm_app.MemoryLinkRequest(
                source_id="mem-source",
                target_id="mem-target",
                relation="unknown_relation",
                project="blackholememory",
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "ontology_link_quarantined"
    assert writes == []


def test_active_ontology_rejects_stale_client_schema_digest(monkeypatch) -> None:
    schema = _schema()
    monkeypatch.setattr(bhm_app, "_active_ontology_schema", lambda _project: schema)
    monkeypatch.setattr(bhm_app, "_find_live_memory", lambda memory_id, _project: _memory(memory_id))

    with pytest.raises(HTTPException) as exc_info:
        bhm_app._create_memory_link(
            bhm_app.MemoryLinkRequest(
                source_id="mem-source",
                target_id="mem-target",
                relation="relates_to",
                project="blackholememory",
                ontology_schema_digest="a" * 64,
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "ontology_schema_digest_mismatch"
