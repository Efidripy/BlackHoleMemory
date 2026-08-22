from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from blackholememory import app as bhm_app


def _record(project: str, content: str) -> dict:
    return {
        "source_id": f"memory-{project}-{content}",
        "project": project,
        "memory_type": "fact",
        "content": content,
        "tags": [],
        "metadata": {"semantic_type": "fact", "lifecycle": "active"},
    }


def test_authoritative_link_adapter_preserves_legacy_metadata_provenance(monkeypatch, tmp_path):
    captured = {}

    class FakeMemoryService:
        path = tmp_path / "memories.sqlite3"

        def list_links(self, *, limit=None):
            assert limit is None
            return [
                {
                    "id": "link_bhm_legacy",
                    "project": "blackholememory",
                    "source_id": "mem-a",
                    "target_id": "mem-b",
                    "relation": "depends_on",
                    "metadata": {
                        "legacy_metadata": {"confidence": 0.4},
                        "sidecar_provenance": {"file": "memory-links.json", "record_sha256": "a" * 64},
                    },
                }
            ]

        def replace_link_records(self, records):
            captured["records"] = records
            return records

    monkeypatch.setattr(bhm_app, "_memory_store_is_authoritative", lambda: True)
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: FakeMemoryService())

    links = bhm_app._load_memory_links()

    assert links[0]["metadata"] == {"confidence": 0.4}
    assert bhm_app._LINK_STORAGE_METADATA_KEY in links[0]
    links[0]["metadata"]["confidence"] = 0.9
    assert bhm_app._save_memory_links(links) == FakeMemoryService.path
    stored = captured["records"][0]
    assert bhm_app._LINK_STORAGE_METADATA_KEY not in stored
    assert stored["metadata"]["legacy_metadata"] == {"confidence": 0.9}
    assert stored["metadata"]["sidecar_provenance"]["file"] == "memory-links.json"


def test_public_search_scope_defaults_to_configured_project_and_reserves_global(monkeypatch):
    monkeypatch.setattr(bhm_app.settings, "qdrant_collection", "blackholememory")

    assert bhm_app._effective_search_project(None) == "blackholememory"
    assert bhm_app._effective_search_project("BlackHoleMemory") == "blackholememory"

    with pytest.raises(HTTPException) as exc_info:
        bhm_app._effective_search_project("GLOBAL")

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "global_scope_requires_explicit_internal_capability"


def test_fallback_search_does_not_turn_missing_project_into_all_projects(monkeypatch):
    monkeypatch.setattr(bhm_app.settings, "qdrant_collection", "blackholememory")
    monkeypatch.setattr(
        bhm_app,
        "_load_live_memories",
        lambda: [_record("blackholememory", "needle A"), _record("other-project", "needle B")],
    )

    response = bhm_app._fallback_grace_mem0_search(
        bhm_app.SearchRequest(query="needle"),
        RuntimeError("provider timeout"),
    )
    records = response["result"]["results"]

    assert [item["metadata"]["project"] for item in records] == ["blackholememory"]


def test_federated_search_filters_each_contour_and_rejects_missing_project_metadata(monkeypatch):
    class FakeEmbedder:
        def embed(self, _query: str, *_args):
            return [1.0]

    class FakeMemory:
        embedding_model = FakeEmbedder()

    def fake_search_memory_collection(*, context_origin: str, **_kwargs):
        project = "blackholememory"
        if context_origin == "LOCAL":
            items = [("local-allowed", project), ("local-wrong", "other-project")]
        else:
            items = [
                ("global-allowed", project),
                ("global-wrong", "other-project"),
                ("global-missing-project", None),
            ]
        return [
            {
                "id": item_id,
                "content": item_id,
                "score": 0.9,
                "context_origin": context_origin,
                "metadata": {"project": item_project, "memory_type": "fact", "tags": [], "files": []},
            }
            for item_id, item_project in items
        ]

    monkeypatch.setattr(bhm_app, "get_project_mem0_memory", lambda _project: FakeMemory())
    monkeypatch.setattr(bhm_app, "_search_memory_collection", fake_search_memory_collection)

    hits, total = asyncio.run(
        bhm_app.federated_search(
            "scope boundary",
            "blackholememory",
            limit=10,
            include_graph_expansion=False,
        )
    )

    assert total == 2
    assert {hit["id"] for hit in hits} == {"local-allowed", "global-allowed"}
    assert all(hit["metadata"]["project"] == "blackholememory" for hit in hits)


def test_advanced_search_without_project_is_scoped_and_missing_vector_metadata_fails_closed(monkeypatch):
    monkeypatch.setattr(bhm_app.settings, "qdrant_collection", "blackholememory")
    monkeypatch.setattr(
        bhm_app,
        "_load_live_memories",
        lambda: [
            _record("blackholememory", "needle A"),
            _record("other-project", "needle B"),
        ],
    )

    memories, total = bhm_app._advanced_search_live_memories(
        bhm_app.MemoryAdvancedSearchRequest(query="needle")
    )
    assert total == 1
    assert [item["project"] for item in memories] == ["blackholememory"]

    request = bhm_app.SearchRequest(query="needle")
    assert not bhm_app._vector_item_matches_search_request(
        {"id": "legacy-without-project", "memory": "needle", "metadata": {}},
        request,
    )


def test_rest_search_rejects_reserved_global_scope_before_provider_work():
    request = bhm_app.MemoryAdvancedSearchRequest(query="needle", project="global")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bhm_app.bhm_search(request))

    assert exc_info.value.status_code == 403


def test_repair_live_indexes_isolates_project_scope(monkeypatch):
    memories = [_record("blackholememory", "present")]
    links = [
        {"source_id": memories[0]["source_id"], "target_id": "missing-a", "project": "BlackHoleMemory"},
        {"source_id": "missing-b", "target_id": "missing-c", "project": "e-github-workspace"},
    ]
    artifacts = [
        {"id": "artifact-a", "project": "BlackHoleMemory", "memory_id": "missing-a"},
        {"id": "artifact-b", "project": "e-github-workspace", "memory_id": "missing-b"},
    ]
    saved_links = {}
    saved_artifacts = {}

    monkeypatch.setattr(bhm_app, "_load_live_memories", lambda: memories)
    monkeypatch.setattr(bhm_app, "_load_memory_links", lambda: links)
    monkeypatch.setattr(bhm_app, "_save_memory_links", lambda value: saved_links.setdefault("items", value))
    for name in (
        "_load_checkpoints",
        "_load_project_maps",
        "_load_adrs",
        "_load_handoffs",
        "_load_session_records",
        "_load_task_contexts",
        "_load_risk_registers",
        "_load_validation_snapshots",
    ):
        monkeypatch.setattr(bhm_app, name, lambda: artifacts)
    for name in (
        "_save_checkpoints",
        "_save_project_maps",
        "_save_adrs",
        "_save_handoffs",
        "_save_session_records",
        "_save_task_contexts",
        "_save_risk_registers",
        "_save_validation_snapshots",
    ):
        monkeypatch.setattr(bhm_app, name, lambda value, key=name: saved_artifacts.setdefault(key, value))

    result = bhm_app._repair_live_indexes(
        bhm_app.RepairLiveIndexesRequest(
            project="BlackHoleMemory",
            remove_orphan_links=True,
            remove_orphan_artifacts=True,
        )
    )

    assert result["project"] == "blackholememory"
    assert result["removed_links"] == 1
    assert [item["project"] for item in saved_links["items"]] == ["e-github-workspace"]
    assert result["removed_artifacts"] == 8
    assert all(
        any(item["project"] == "e-github-workspace" for item in value)
        for value in saved_artifacts.values()
    )


def test_repair_live_indexes_requires_project_or_explicit_aggregate():
    with pytest.raises(HTTPException) as exc_info:
        bhm_app._repair_live_indexes(bhm_app.RepairLiveIndexesRequest())

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "repair_scope_required"


def test_strict_repair_forwards_project_scope(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        bhm_app,
        "_normalize_memory_metadata",
        lambda project: captured.setdefault("normalize", project) or {"updated": 0},
    )
    monkeypatch.setattr(
        bhm_app,
        "_repair_live_indexes",
        lambda request: captured.setdefault("repair", request) or {"removed_links": 0},
    )
    monkeypatch.setattr(
        bhm_app,
        "_schema_validate_strict",
        lambda request: captured.setdefault("validate", request) or {"ok": True},
    )

    result = bhm_app._integrity_repair_strict(
        bhm_app.IntegrityRepairStrictRequest(project="BlackHoleMemory")
    )

    assert result["project"] == "blackholememory"
    assert captured["normalize"] == "blackholememory"
    assert captured["repair"].project == "blackholememory"
    assert captured["repair"].aggregate is False
    assert captured["validate"].project == "blackholememory"


def test_hard_delete_preserves_foreign_project_references(monkeypatch):
    target = _record("blackholememory", "target")
    monkeypatch.setattr(bhm_app, "_delete_live_memory", lambda _request: target)
    links = [
        {"source_id": target["source_id"], "target_id": "a", "project": "BlackHoleMemory"},
        {"source_id": target["source_id"], "target_id": "b", "project": "e-github-workspace"},
    ]
    saved_links = {}
    monkeypatch.setattr(bhm_app, "_load_memory_links", lambda: links)
    monkeypatch.setattr(bhm_app, "_save_memory_links", lambda value: saved_links.setdefault("items", value))

    artifact_stores = {}
    for loader_name, saver_name in (
        ("_load_checkpoints", "_save_checkpoints"),
        ("_load_project_maps", "_save_project_maps"),
        ("_load_adrs", "_save_adrs"),
        ("_load_handoffs", "_save_handoffs"),
        ("_load_session_records", "_save_session_records"),
        ("_load_tasks", "_save_tasks"),
        ("_load_task_contexts", "_save_task_contexts"),
        ("_load_risk_registers", "_save_risk_registers"),
        ("_load_validation_snapshots", "_save_validation_snapshots"),
    ):
        monkeypatch.setattr(
            bhm_app,
            loader_name,
            lambda: [
                {"id": "a-artifact", "memory_id": target["source_id"], "project": "blackholememory"},
                {"id": "b-artifact", "memory_id": target["source_id"], "project": "e-github-workspace"},
            ],
        )
        monkeypatch.setattr(
            bhm_app,
            saver_name,
            lambda value, key=saver_name: artifact_stores.setdefault(key, value),
        )

    result = bhm_app._delete_live_memory_hard(
        bhm_app.HardDeleteMemoryRequest(id=target["source_id"], project="BlackHoleMemory")
    )

    assert result["project"] == "blackholememory"
    assert [item["project"] for item in saved_links["items"]] == ["e-github-workspace"]
    assert all(
        [item["project"] for item in items] == ["e-github-workspace"]
        for items in artifact_stores.values()
    )


def test_refresh_all_requires_explicit_scope_or_aggregate(monkeypatch):
    with pytest.raises(HTTPException) as exc_info:
        bhm_app._project_summary_refresh_all(bhm_app.ProjectSummaryRefreshAllRequest())
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "refresh_scope_required"

    refreshed = []
    monkeypatch.setattr(
        bhm_app,
        "_load_live_memories",
        lambda: [
            {"source_id": "a", "project": "BlackHoleMemory"},
            {"source_id": "b", "project": "e-github-workspace"},
            {"source_id": "a2", "project": "blackholememory"},
        ],
    )
    monkeypatch.setattr(
        bhm_app,
        "_rebuild_project_summary",
        lambda request: refreshed.append(request.project) or {"project": request.project},
    )

    result = bhm_app._project_summary_refresh_all(
        bhm_app.ProjectSummaryRefreshAllRequest(aggregate=True)
    )

    assert result["aggregate"] is True
    assert result["projects"] == ["blackholememory", "e-github-workspace"]
    assert refreshed == result["projects"]


def test_rebuild_project_summary_keeps_canonical_discoverability_with_custom_key(monkeypatch):
    captured = {}
    for name in (
        "_get_project_map",
        "_get_latest_checkpoint",
        "_get_task_context",
        "_get_risk_register",
        "_get_validation_snapshot",
    ):
        monkeypatch.setattr(bhm_app, name, lambda _project: None)

    def fake_upsert(request):
        captured["request"] = request
        return "created", {"source_id": "summary-1", "project": request.project}

    monkeypatch.setattr(bhm_app, "_upsert_live_memory", fake_upsert)
    monkeypatch.setattr(bhm_app, "_serialize_memory_record", lambda record: record)

    result = bhm_app._rebuild_project_summary(
        bhm_app.RebuildProjectSummaryRequest(
            project="BlackHoleMemory",
            upsert_key="custom-summary-key",
        )
    )

    request = captured["request"]
    assert request.project == "blackholememory"
    assert request.upsert_key == "project-summary:blackholememory"
    assert result["canonical_upsert_key"] == request.upsert_key
    assert result["requested_upsert_key"] == "custom-summary-key"
