from __future__ import annotations

import asyncio

from blackholememory import app as bhm_app
from blackholememory.retrieval_filters import build_candidate_filters


def test_candidate_filters_push_project_and_taxonomy_predicates_downstream():
    filters = build_candidate_filters(
        user_id="user-1",
        project_values={"blackholememory", "BlackHoleMemory"},
        memory_type="knowledge",
        concepts=["qdrant"],
        files=["README.md"],
        domain="infra",
        semantic_type="architecture",
        priority="high",
        include_archived=False,
        include_logs=False,
    )

    assert filters["user_id"] == "user-1"
    assert {"project": {"in": ["BlackHoleMemory", "blackholememory"]}} in filters["AND"]
    assert {"memory_type": "knowledge"} in filters["AND"]
    assert {"tags": ["qdrant"]} in filters["AND"]
    assert {"files": ["README.md"]} in filters["AND"]
    assert {"domain": "infra"} in filters["AND"]
    assert {"semantic_type": "architecture"} in filters["AND"]
    assert {"priority": "high"} in filters["AND"]
    assert {"lifecycle": {"in": ["archived", "deprecated"]}} in filters["NOT"]
    assert {"semantic_type": {"in": ["log", "error"]}} in filters["NOT"]


def test_candidate_filters_keep_user_scope_when_optional_filters_are_empty():
    assert build_candidate_filters(user_id="user-1") == {
        "user_id": "user-1",
        "NOT": [
            {"lifecycle": {"in": ["archived", "deprecated"]}},
            {"semantic_type": {"in": ["log", "error"]}},
        ],
    }


def test_federated_search_pushes_the_same_project_boundary_to_both_contours(monkeypatch):
    captured: dict[str, dict] = {}

    class FakeEmbedder:
        def embed(self, _query: str, *_args):
            return [1.0]

    class FakeMemory:
        embedding_model = FakeEmbedder()

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    def fake_search_memory_collection(*, context_origin: str, candidate_filters: dict, **_kwargs):
        captured[context_origin] = candidate_filters
        return []

    monkeypatch.setattr(bhm_app, "get_project_mem0_memory", lambda _project: FakeMemory())
    monkeypatch.setattr(bhm_app, "_search_memory_collection", fake_search_memory_collection)
    monkeypatch.setattr(bhm_app.asyncio, "to_thread", fake_to_thread)

    hits, total = asyncio.run(
        bhm_app.federated_search(
            "scope boundary",
            "BlackHoleMemory",
            limit=5,
            include_graph_expansion=False,
            include_global=True,
        )
    )

    assert hits == []
    assert total == 0
    assert set(captured) == {"LOCAL", "GLOBAL"}
    expected_projects = sorted(bhm_app._project_aliases("BlackHoleMemory"))
    for filters in captured.values():
        assert filters["user_id"] == bhm_app.settings.mem0_user_id
        assert {"project": {"in": expected_projects}} in filters["AND"]
