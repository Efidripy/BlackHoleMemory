import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from blackholememory.api_models import MemoryAdvancedSearchRequest
from blackholememory.bhm_mcp_memory import bhm_search
from blackholememory.caller_auth import CallerPrincipal
from blackholememory.memory_search_multiproject import (
    multiproject_search,
    resolve_retrieval_projects,
)
from blackholememory.memory_search_service import MemorySearchDependencies, MemorySearchService


def make_request(projects=None, query="test query", limit=10):
    return SimpleNamespace(
        projects=projects,
        query=query,
        limit=limit,
        offset=0,
        memory_type=None,
        concepts=None,
        files=None,
        domain=None,
        semantic_type=None,
        priority=None,
        include_archived=False,
        include_logs=False,
    )


def test_wildcard_resolution_filters_service_and_empty_projects():
    mock_service = MagicMock()
    mock_service.list_projects.return_value = [
        "demo-a",
        "bhm-surface-smoke-test1",
        "empty-proj",
        "demo-b",
    ]

    def count_records(project=None, include_archived=False):
        if project == "empty-proj":
            return 0
        return 3

    mock_service.count_records.side_effect = count_records

    resolved = resolve_retrieval_projects(["*"], lambda: mock_service)
    assert resolved == ["demo-a", "demo-b"]


def test_wildcard_resolution_intersects_with_caller_allowed_projects():
    mock_service = MagicMock()
    mock_service.list_projects.return_value = ["demo-a", "demo-b", "demo-c"]
    mock_service.count_records.return_value = 5

    scoped_principal = CallerPrincipal(
        caller_id="test-operator",
        allowed_projects=frozenset(["demo-a", "demo-c"]),
        default_project="demo-a",
        all_projects=False,
    )

    with patch(
        "blackholememory.memory_search_multiproject.configured_caller_principal",
        return_value=scoped_principal,
    ):
        resolved = resolve_retrieval_projects(["*"], lambda: mock_service)
        assert resolved == ["demo-a", "demo-c"]


def test_explicit_project_list_resolution():
    mock_service = MagicMock()
    resolved = resolve_retrieval_projects(["demo-x", "demo-y"], lambda: mock_service)
    assert resolved == ["demo-x", "demo-y"]
    mock_service.list_projects.assert_not_called()


def test_forbidden_project_raises_403_and_searches_nothing():
    calls = []

    async def search_one(project):
        calls.append(project)
        return []

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            multiproject_search(
                make_request(["secret"]),
                resolve_projects=lambda value: value,
                search_one=search_one,
                authorize=lambda projects: "caller_project_forbidden",
            )
        )
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "caller_project_forbidden"
    assert calls == []


def test_wildcard_filters_forbidden_projects_without_403():
    async def search_one(project):
        return [{"id": f"{project}-1", "score": 0.8}]

    def authorize(projects):
        if any(p == "secret" for p in projects):
            return "caller_project_forbidden"
        return None

    result = asyncio.run(
        multiproject_search(
            make_request(["*"]),
            resolve_projects=lambda value: ["allowed-a", "secret", "allowed-b"],
            search_one=search_one,
            authorize=authorize,
        )
    )
    assert [item["project"] for item in result["memories"]] == ["allowed-a", "allowed-b"]
    assert result["cross_project"]["projects_searched"] == 2


def test_merging_and_ordering_with_lexical_tie_break():
    async def search_one(project):
        if project == "p1":
            return [
                {"id": "hit-high", "score": 0.9, "lexical_rank": 10},
                {"id": "hit-tie-b", "score": 0.5, "lexical_rank": 2},
            ]
        return [
            {"id": "hit-tie-a", "score": 0.5, "lexical_rank": 1},
            {"id": "hit-low", "score": 0.2, "lexical_rank": 1},
        ]

    result = asyncio.run(
        multiproject_search(
            make_request(["p1", "p2"]),
            resolve_projects=lambda value: value,
            search_one=search_one,
            authorize=None,
        )
    )
    hit_ids = [m["id"] for m in result["memories"]]
    assert hit_ids == ["hit-high", "hit-tie-a", "hit-tie-b", "hit-low"]


def test_limit_applied_to_merged_results():
    async def search_one(project):
        return [
            {"id": f"{project}-1", "score": 0.9},
            {"id": f"{project}-2", "score": 0.8},
        ]

    result = asyncio.run(
        multiproject_search(
            make_request(["a", "b"], limit=3),
            resolve_projects=lambda value: value,
            search_one=search_one,
            authorize=None,
        )
    )
    assert len(result["memories"]) == 3
    assert result["total"] == 4


def test_skip_failed_and_timed_out_projects_respecting_deadline():
    async def search_one(project):
        if project == "failed":
            raise RuntimeError("disk read error")
        if project == "slow":
            await asyncio.sleep(0.3)
            return [{"id": "slow-1", "score": 0.7}]
        await asyncio.sleep(0.01)
        return [{"id": "fast-1", "score": 0.95}]

    start = time.monotonic()
    result = asyncio.run(
        multiproject_search(
            make_request(["fast", "slow", "failed"], limit=10),
            resolve_projects=lambda value: value,
            search_one=search_one,
            authorize=None,
            deadline_s=0.08,
            concurrency=3,
        )
    )
    duration = time.monotonic() - start

    assert duration < 0.25
    assert len(result["memories"]) == 1
    assert result["memories"][0]["project"] == "fast"
    assert result["memories"][0]["id"] == "fast-1"

    skipped_map = {item["project"]: item["reason"] for item in result["cross_project"]["skipped"]}
    assert skipped_map["failed"] == "error"
    assert skipped_map["slow"] == "timeout"


def test_capacity_exceeded_retries_before_returning_project_hits():
    from blackholememory.app_core_fallback import RetrievalProviderCapacityExceeded

    attempts = 0

    async def search_one(project):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise RetrievalProviderCapacityExceeded("provider busy")
        return [{"id": f"{project}-1", "score": 0.8}]

    result = asyncio.run(
        multiproject_search(
            make_request(["busy"]),
            resolve_projects=lambda value: value,
            search_one=search_one,
            authorize=None,
            deadline_s=0.5,
        )
    )

    assert attempts == 3
    assert result["memories"][0]["project"] == "busy"
    assert result["cross_project"]["skipped"] == []
    assert result["cross_project"]["project_diagnostics"]["busy"]["attempts"] == 3
    assert result["cross_project"]["project_diagnostics"]["busy"]["status"] == "hits"


def test_capacity_exceeded_is_timeout_when_deadline_expires():
    from blackholememory.app_core_fallback import RetrievalProviderCapacityExceeded

    attempts = 0

    async def search_one(_project):
        nonlocal attempts
        attempts += 1
        raise RetrievalProviderCapacityExceeded("provider busy")

    result = asyncio.run(
        multiproject_search(
            make_request(["busy"]),
            resolve_projects=lambda value: value,
            search_one=search_one,
            authorize=None,
            deadline_s=0.03,
        )
    )

    assert attempts >= 1
    assert result["cross_project"]["skipped"] == [{"project": "busy", "reason": "timeout"}]


def test_project_labels_and_cross_project_block():
    async def search_one(project):
        if project == "empty":
            return []
        return [
            {"id": f"{project}-1", "score": 0.7},
            {"id": f"{project}-2", "score": 0.9},
        ]

    result = asyncio.run(
        multiproject_search(
            make_request(["has-hits", "empty"]),
            resolve_projects=lambda value: value,
            search_one=search_one,
            authorize=None,
        )
    )

    for item in result["memories"]:
        assert item["project"] == "has-hits"

    cross = result["cross_project"]
    assert cross["projects_searched"] == 2
    assert cross["projects_with_hits"] == 1
    assert cross["per_project_top"] == {"has-hits": 0.9}
    assert cross["skipped"] == []


def test_concurrency_bounding():
    active = 0
    peak = 0

    async def search_one(project):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.03)
        active -= 1
        return [{"id": project, "score": 0.5}]

    projects = [f"proj-{i}" for i in range(6)]
    asyncio.run(
        multiproject_search(
            make_request(projects),
            resolve_projects=lambda value: value,
            search_one=search_one,
            authorize=None,
            concurrency=2,
        )
    )
    assert peak <= 2


def test_service_wiring_single_project_without_projects():
    federated_calls = []

    async def mock_federated_search(query, project, **kwargs):
        federated_calls.append((query, project, kwargs))
        return ([{"id": "mem-1", "score": 0.9}], 1)

    deps = MemorySearchDependencies(
        ensure_provider_warmup_ready=MagicMock(return_value=asyncio.sleep(0)),
        effective_search_project=lambda p: p or "default-p",
        federated_search=mock_federated_search,
        advanced_search=MagicMock(),
        serialize_vector_hit=lambda hit: hit,
        emit_memory_pulses=MagicMock(),
        is_fallback_grace_error=lambda exc: False,
        fallback_grace_memories_response=MagicMock(),
        local_collection_name=lambda p: f"local-{p}",
        global_collection_name=lambda: "global-coll",
    )
    service = MemorySearchService(deps)

    req = MemoryAdvancedSearchRequest(query="search something", project="demo-a", projects=None)
    response = asyncio.run(service.execute(req))

    assert len(federated_calls) == 1
    query, project, kwargs = federated_calls[0]
    assert query == "search something"
    assert project == "demo-a"
    assert kwargs.get("include_global") is not False
    assert "include_graph_expansion" not in kwargs
    assert "candidate_pool" not in kwargs
    assert response["retrieval"]["mode"] == "federated"


def test_service_wiring_multiproject():
    federated_calls = []

    async def mock_federated_search(query, project, **kwargs):
        federated_calls.append((query, project, kwargs))
        return ([{"id": f"{project}-1", "score": 0.85}], 1)

    deps = MemorySearchDependencies(
        ensure_provider_warmup_ready=MagicMock(return_value=asyncio.sleep(0)),
        effective_search_project=lambda p: p or "default-p",
        federated_search=mock_federated_search,
        advanced_search=MagicMock(),
        serialize_vector_hit=lambda hit: hit,
        emit_memory_pulses=MagicMock(),
        is_fallback_grace_error=lambda exc: False,
        fallback_grace_memories_response=MagicMock(),
        local_collection_name=lambda p: f"local-{p}",
        global_collection_name=lambda: "global-coll",
        resolve_projects=lambda projs: projs,
        authorize_projects=lambda projs: None,
    )
    service = MemorySearchService(deps)

    req = MemoryAdvancedSearchRequest(
        query="cross search",
        projects=["demo-1", "demo-2"],
    )
    response = asyncio.run(service.execute(req))

    assert len(federated_calls) == 2
    for _q, _p, kwargs in federated_calls:
        assert kwargs.get("include_global") is False
        assert kwargs["include_graph_expansion"] is False
        assert kwargs["limit"] == 5
    assert response["retrieval"]["mode"] == "multiproject"
    assert response["retrieval"]["include_global"] is False
    assert "cross_project" in response
    assert response["cross_project"]["projects_searched"] == 2
    assert "answerability" in response


def test_mcp_wrapper_projects_csv():
    posts = []

    def mock_post(url, body):
        posts.append((url, body))
        return {"ok": True}

    with patch("blackholememory.bhm_mcp_memory._base._post", side_effect=mock_post):
        bhm_search("query text", projects_csv="demo-a,demo-b")
        assert posts[-1][1]["projects"] == ["demo-a", "demo-b"]

        bhm_search("query text", projects_csv="*")
        assert posts[-1][1]["projects"] == ["*"]

        bhm_search("query text", projects_csv=None)
        assert "projects" not in posts[-1][1]


def test_service_wiring_multiproject_accepts_federated_outcome_dataclass():
    from blackholememory.app_core_fallback import FederatedSearchOutcome

    async def mock_federated_search(query, project, **kwargs):
        return FederatedSearchOutcome(hits=[{"id": f"{project}-1", "score": 0.85}], total=1, contour_trace={})

    deps = MemorySearchDependencies(
        ensure_provider_warmup_ready=MagicMock(return_value=asyncio.sleep(0)),
        effective_search_project=lambda p: p or "default-p",
        federated_search=mock_federated_search,
        advanced_search=MagicMock(),
        serialize_vector_hit=lambda hit: hit,
        emit_memory_pulses=MagicMock(),
        is_fallback_grace_error=lambda exc: False,
        fallback_grace_memories_response=MagicMock(),
        local_collection_name=lambda p: f"local-{p}",
        global_collection_name=lambda: "global-coll",
        resolve_projects=lambda projs: projs,
        authorize_projects=lambda projs: None,
    )
    request = MemoryAdvancedSearchRequest(query="q", projects=["demo-a", "demo-b"], limit=5)
    response = asyncio.run(MemorySearchService(deps).execute(request))
    assert response["cross_project"]["projects_with_hits"] == 2
    assert not response["cross_project"]["skipped"]
