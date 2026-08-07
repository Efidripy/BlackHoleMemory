from __future__ import annotations

import asyncio
from types import SimpleNamespace

from blackholememory.memory_search_service import MemorySearchDependencies
from blackholememory.memory_search_service import MemorySearchService


def _request(**overrides):
    values = {
        "query": "needle",
        "project": "demo",
        "limit": 5,
        "offset": 0,
        "memory_type": None,
        "concepts": None,
        "files": None,
        "domain": None,
        "semantic_type": None,
        "priority": None,
        "include_archived": False,
        "include_logs": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _service(*, federated_search, advanced_search=lambda request: _async_value({"advanced": True})):
    async def ready():
        return None

    async def _advanced(request):
        return await advanced_search(request)

    return MemorySearchService(
        MemorySearchDependencies(
            ensure_provider_warmup_ready=ready,
            effective_search_project=lambda project: project or "demo",
            federated_search=federated_search,
            advanced_search=_advanced,
            serialize_vector_hit=lambda item: {"id": item["id"]},
            emit_memory_pulses=lambda items: None,
            is_fallback_grace_error=lambda exc: isinstance(exc, TimeoutError),
            fallback_grace_memories_response=lambda route, reason, **kwargs: {
                "fallback_grace": {"route": route, "reason": type(reason).__name__},
                "memories": [],
            },
            local_collection_name=lambda project: f"local-{project}",
            global_collection_name=lambda: "global",
        )
    )


async def _async_value(value):
    return value


def test_empty_query_delegates_to_advanced_search():
    calls = []

    async def advanced(request):
        calls.append(request.query)
        return {"advanced": True}

    service = _service(federated_search=lambda *args, **kwargs: None, advanced_search=advanced)
    result = asyncio.run(service.execute(_request(query="   ")))

    assert result["advanced"] is True
    assert result["side_effects"] == {
        "read_only": True,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "projection_mutation": False,
    }
    assert calls == ["   "]


def test_federated_result_and_fallback_preserve_response_policy():
    async def federated(query, project, **kwargs):
        return ([{"id": "m1"}], 1)

    service = _service(federated_search=federated)
    result = asyncio.run(service.execute(_request()))

    assert result["memories"] == [{"id": "m1"}]
    assert result["retrieval"]["mode"] == "federated"
    assert result["filters"]["project"] == "demo"
    assert result["side_effects"]["read_only"] is True
    assert result["side_effects"]["sqlite_mutation"] is False
    assert result["side_effects"]["qdrant_mutation"] is False

    async def failing(*args, **kwargs):
        raise TimeoutError("provider timeout")

    degraded = _service(federated_search=failing)
    fallback = asyncio.run(degraded.execute(_request()))
    assert fallback["fallback_grace"]["route"] == "bhm.search.federated"
    assert fallback["retrieval"]["mode"] == "federated-fallback-grace"
    assert fallback["side_effects"]["projection_mutation"] is False
