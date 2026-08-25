from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

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
    assert {"event_role": "trace"} in filters["NOT"]


def test_candidate_filters_keep_user_scope_when_optional_filters_are_empty():
    assert build_candidate_filters(user_id="user-1") == {
        "user_id": "user-1",
        "NOT": [
            {"lifecycle": {"in": ["archived", "deprecated"]}},
            {"semantic_type": {"in": ["log", "error"]}},
            {"event_role": "trace"},
        ],
    }


def test_candidate_filters_allow_explicit_historical_or_trace_routes():
    assert {"event_role": "trace"} not in build_candidate_filters(
        user_id="user-1", include_historical=True
    )["NOT"]
    assert {"event_role": "trace"} not in build_candidate_filters(
        user_id="user-1", event_role="trace"
    )["NOT"]


def test_authoritative_post_filter_excludes_trace_unless_history_is_explicit():
    trace = {
        "project": "blackholememory",
        "memory_type": "workflow",
        "event_role": "trace",
        "metadata": {"event_role": "trace"},
    }

    assert bhm_app._memory_matches_filters(trace, project="blackholememory") is False
    assert bhm_app._memory_matches_filters(
        trace, project="blackholememory", include_historical=True
    ) is True
    assert bhm_app._memory_matches_filters(
        trace, project="blackholememory", event_role="trace"
    ) is True


def test_memory_listing_and_fallback_keep_trace_history_opt_in(monkeypatch):
    current = {
        "source_id": "mem-current",
        "project": "blackholememory",
        "memory_type": "decision",
        "event_role": "decision",
        "metadata": {"event_role": "decision"},
    }
    trace = {
        "source_id": "mem-trace",
        "project": "blackholememory",
        "memory_type": "workflow",
        "event_role": "trace",
        "metadata": {"event_role": "trace", "artifact_kind": "session-record"},
    }
    monkeypatch.setattr(bhm_app, "_load_live_memories", lambda: [current, trace])

    current_window, current_total, _limit, _offset = bhm_app._list_live_memories(
        project="blackholememory",
        memory_type=None,
        include_archived=False,
        include_historical=False,
        limit=20,
        offset=0,
    )
    history_window, history_total, _limit, _offset = bhm_app._list_live_memories(
        project="blackholememory",
        memory_type=None,
        include_archived=False,
        include_historical=True,
        limit=20,
        offset=0,
    )

    assert current_total == 1
    assert [item["source_id"] for item in current_window] == ["mem-current"]
    assert history_total == 2
    assert {item["source_id"] for item in history_window} == {"mem-current", "mem-trace"}
    assert [item["source_id"] for item in bhm_app._fallback_memory_records(project="blackholememory")] == ["mem-current"]
    assert {item["source_id"] for item in bhm_app._fallback_memory_records(
        project="blackholememory", include_historical=True
    )} == {"mem-current", "mem-trace"}


def test_advanced_search_freshness_filters_updated_at_before_pagination(monkeypatch):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    old = (now - timedelta(days=45)).isoformat().replace("+00:00", "Z")
    records = [
        {
            "source_id": "mem-recent-trace",
            "project": "blackholememory",
            "memory_type": "workflow",
            "event_role": "trace",
            "content": "recent workflow receipt",
            "updated_at": recent,
            "metadata": {"event_role": "trace", "priority": "high"},
        },
        {
            "source_id": "mem-old-trace",
            "project": "blackholememory",
            "memory_type": "workflow",
            "event_role": "trace",
            "content": "old workflow receipt",
            "updated_at": old,
            "metadata": {"event_role": "trace", "priority": "high"},
        },
    ]
    monkeypatch.setattr(bhm_app, "_load_live_memories", lambda: records)

    window, total = bhm_app._advanced_search_live_memories(
        bhm_app.MemoryAdvancedSearchRequest(
            query="workflow receipt",
            project="blackholememory",
            include_historical=True,
            freshness_days=7,
            priority="high",
            limit=1,
        )
    )

    assert total == 1
    assert [item["source_id"] for item in window] == ["mem-recent-trace"]


def test_freshness_days_contract_is_bounded():
    with pytest.raises(ValueError):
        bhm_app.MemoryAdvancedSearchRequest(freshness_days=0)
    with pytest.raises(ValueError):
        bhm_app.MemoryAdvancedSearchRequest(freshness_days=3651)


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
