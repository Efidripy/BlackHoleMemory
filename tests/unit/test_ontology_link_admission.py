from __future__ import annotations

import pytest
from fastapi import HTTPException

from blackholememory import app as bhm_app
from blackholememory.ontology_registry import OntologyEntityType
from blackholememory.ontology_registry import OntologyRelationType
from blackholememory.ontology_registry import OntologySchema


class _ArtifactService:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict] = {}
        self.append_calls = 0
        self.list_calls: list[tuple[str | None, str | None, int | None]] = []

    def append_artifact(self, artifact):
        self.append_calls += 1
        key = (artifact.artifact_type, artifact.id)
        if key in self.records:
            return self.records[key], False
        record = artifact.to_record()
        self.records[key] = record
        return record, True

    def list_artifact_records(self, *, artifact_type=None, project=None, limit=None, **_kwargs):
        self.list_calls.append((artifact_type, project, limit))
        records = [
            item for item in self.records.values()
            if (project is None or item.get("project") == project)
        ]
        return records[:limit]


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
    artifacts = _ArtifactService()

    monkeypatch.setattr(bhm_app, "_active_ontology_schema", lambda _project: schema)
    monkeypatch.setattr(bhm_app, "_find_live_memory", lambda memory_id, _project: _memory(memory_id))
    monkeypatch.setattr(bhm_app, "_load_memory_links", lambda: [])
    monkeypatch.setattr(bhm_app, "_save_memory_links", lambda links: writes.append(links))
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: artifacts)

    request = bhm_app.MemoryLinkRequest(
        source_id="mem-source",
        target_id="mem-target",
        relation="unknown_relation",
        project="blackholememory",
    )
    for expected_stored in (True, False):
        with pytest.raises(HTTPException) as exc_info:
            bhm_app._create_memory_link(request)

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "ontology_link_quarantined"
        quarantine = exc_info.value.detail["quarantine"]
        assert quarantine["stored"] is expected_stored
        assert quarantine["requires_review"] is True
        assert quarantine["execution"] == {
            "link_storage_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
        }
        assert "mem-source" not in str(quarantine)
        assert "mem-target" not in str(quarantine)

    assert artifacts.append_calls == 2
    assert len(artifacts.records) == 1
    assert writes == []


def test_ontology_quarantine_list_is_project_scoped_and_content_free(monkeypatch) -> None:
    artifacts = _ArtifactService()
    artifacts.records[("ontology_quarantine", "local")] = {
        "id": "local",
        "project": "blackholememory",
        "schema_version": "bhm.ontology-quarantine.v1",
        "event_digest": "a" * 64,
        "schema_digest": "b" * 64,
        "reason_code": "ontology_relation_unknown",
        "relation": "unknown_relation",
        "source_id_digest": "c" * 64,
        "target_id_digest": "d" * 64,
        "content_free": True,
        "requires_review": True,
        "review_state": "open",
    }
    artifacts.records[("ontology_quarantine", "foreign")] = {
        **artifacts.records[("ontology_quarantine", "local")],
        "id": "foreign",
        "project": "e-github-workspace",
    }
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: artifacts)

    result = bhm_app._list_ontology_quarantine("blackholememory", 500)

    assert result["project"] == "blackholememory"
    assert result["count"] == 1
    assert result["limit"] == 200
    assert result["items"][0]["id"] == "local"
    assert artifacts.list_calls == [("ontology_quarantine", "blackholememory", 200)]
    assert result["execution"] == {
        "sqlite_mutation": False,
        "link_storage_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
    }
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


def test_merge_preview_reports_ontology_affected_links_and_never_applies(monkeypatch) -> None:
    source = {"source_id": "mem-source", "project": "blackholememory", "content": "source", "metadata": {}}
    target = {"source_id": "mem-target", "project": "blackholememory", "content": "target", "metadata": {}}
    records = {"mem-source": source, "mem-target": target}
    links = [
        {
            "id": "link-outgoing",
            "project": "blackholememory",
            "source_id": "mem-source",
            "target_id": "mem-target",
            "relation": "relates_to",
            "metadata": {"ontology": {"schema_digest": "a" * 64, "revision": 1, "admission": "allow"}},
        },
        {
            "id": "link-incoming",
            "project": "blackholememory",
            "source_id": "mem-target",
            "target_id": "mem-source",
            "relation": "relates_to",
            "metadata": {"ontology": {"schema_digest": "a" * 64, "revision": 1, "admission": "allow"}},
        },
    ]
    monkeypatch.setattr(bhm_app, "_find_live_memory", lambda memory_id, _project: records.get(memory_id))
    monkeypatch.setattr(bhm_app, "_load_memory_links", lambda: links)

    result = bhm_app._memory_merge_preview(
        bhm_app.MemoryMergePreviewRequest(project="blackholememory", source_id="mem-source", target_id="mem-target")
    )

    assert result["resolution"]["schema_version"] == "bhm.memory-merge-preview.v2"
    assert result["resolution"]["affected_link_count"] == 2
    assert result["resolution"]["self_link_count"] == 2
    assert result["resolution"]["collision_count"] == 1
    assert result["resolution"]["affected_links"][0]["ontology"]["admission"] == "allow"
    assert len(result["resolution"]["plan_digest"]) == 64
    assert result["rollback"]["apply_performed"] is False
    assert result["execution"] == {"sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False}
