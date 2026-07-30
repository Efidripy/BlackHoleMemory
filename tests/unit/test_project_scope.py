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
