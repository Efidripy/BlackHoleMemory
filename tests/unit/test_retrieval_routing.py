from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from blackholememory.memory_search_compact import (
    compact_search_response,
    finalize_search_response,
)
from blackholememory.memory_search_service import (
    MemorySearchDependencies,
    MemorySearchService,
)
from blackholememory.retrieval_routing import (
    CODE_GRAPH_TOOLS,
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    DOCS_TOOLS,
    GIT_TOOLS,
    SOURCE_CODE_GRAPH,
    SOURCE_DOCS,
    SOURCE_GIT,
    SOURCE_MEMORY,
    classify_query,
)


CODE_GRAPH_QUERIES = [
    # Russian questions (>= 8)
    "структура проекта",
    "архитектура приложения",
    "как устроен проект",
    "где в коде создается подключение к бд",
    "где реализована функция classify_query",
    "в каком файле находится MemorySearchService",
    "какая функция отвечает за поиск",
    "кто вызывает assess_answerability",
    "что вызывает build_retrieval_query_plan",
    "зависимости модуля blackholememory",
    "граф вызовов для memory_search",
    # English questions
    "what is the project structure?",
    "how is the project structured?",
    "where in code is the database client created?",
    "where is MemorySearchService implemented?",
    "which file contains the search logic?",
    "which function handles query routing?",
    "who calls assess_answerability?",
    "what calls the embedding model?",
    "module dependencies of the search engine",
    "show call graph for retrieval engine",
]

GIT_QUERIES = [
    # Russian questions (>= 4)
    "последний коммит в репозитории",
    "кто изменил конфигурацию аутентификации",
    "когда добавили поддержку qdrant",
    "история изменений схемы хранения",
    "в каком релизе появилось кэширование эмбеддингов",
    "какой коммит сломал сборку",
    # English questions (>= 4)
    "what was the last commit?",
    "who modified the auth policy?",
    "when was the feature added?",
    "show git log for this branch",
    "run git blame on lines 10-20",
    "show git diff between branches",
    "which version introduced the new API?",
]

DOCS_QUERIES = [
    # Russian questions (>= 4)
    "где лежит README?",
    "runbook по устранению инцидентов",
    "список ADR по принятым решениям",
    "документация по API сервиса",
    "инструкция из репозитория по установке",
    "changelog последней версии",
    # English questions (>= 4)
    "where is the project documentation?",
    "read the operational runbook",
    "review ADR-005 record",
    "check changelog for recent updates",
    "instructions from the repo for local setup",
]

MEMORY_QUERIES = [
    # 4 mandatory queries
    "Как правильно деплоить на Ubuntu?",
    "Как перезапустить API памяти?",
    "Почему smoke релиза падал?",
    "Что решили про подпись релизов?",
    # Additional Russian questions
    "грабли при настройке systemd",
    "что делать если упал qdrant",
    "почему решили перейти на hybrid search",
    "как сделать бэкап базы",
    "какие соглашения по именованию проектов приняты",
    "почему падает воркер при высокой нагрузке",
    "как перезапустить сервис",
    # English questions
    "how to properly deploy on Ubuntu",
    "why did the release smoke test fail",
    "what was decided about release signing",
    "how to restart the memory service",
    "troubleshooting guide for database timeout",
    "gotchas with docker memory limits",
    "what to do if connection pool exhausts",
]

FILE_EXTENSION_QUERIES = [
    "memory_search_compact.py",
    "Service.cs",
    "deploy.ps1",
    "index.js",
    "worker.ts",
    "schema.json",
    "В файле retrieval_routing.py что изменить?",
    "what does main.py do?",
]


@pytest.mark.parametrize("query", CODE_GRAPH_QUERIES)
def test_classify_code_graph_queries(query: str) -> None:
    result = classify_query(query)
    assert result["suggested_source"] == SOURCE_CODE_GRAPH
    assert result["confidence"] == CONFIDENCE_HIGH
    assert result["tools"] == CODE_GRAPH_TOOLS
    assert "routing" not in result  # routing is the envelope in finalize_search_response
    assert "intent" in result
    assert "reason" in result
    assert result.get("hint")


@pytest.mark.parametrize("query", GIT_QUERIES)
def test_classify_git_queries(query: str) -> None:
    result = classify_query(query)
    assert result["suggested_source"] == SOURCE_GIT
    assert result["confidence"] == CONFIDENCE_HIGH
    assert result["tools"] == GIT_TOOLS
    assert result.get("hint")


@pytest.mark.parametrize("query", DOCS_QUERIES)
def test_classify_docs_queries(query: str) -> None:
    result = classify_query(query)
    assert result["suggested_source"] == SOURCE_DOCS
    assert result["confidence"] == CONFIDENCE_HIGH
    assert result["tools"] == DOCS_TOOLS
    assert result.get("hint")


@pytest.mark.parametrize("query", MEMORY_QUERIES)
def test_classify_memory_queries(query: str) -> None:
    result = classify_query(query)
    assert result["suggested_source"] == SOURCE_MEMORY
    assert result["confidence"] == CONFIDENCE_LOW
    assert "intent" in result


@pytest.mark.parametrize("query", FILE_EXTENSION_QUERIES)
def test_classify_file_extension_queries(query: str) -> None:
    result = classify_query(query)
    assert result["suggested_source"] == SOURCE_CODE_GRAPH
    assert result["confidence"] == CONFIDENCE_HIGH
    assert result["tools"] == CODE_GRAPH_TOOLS


def test_empty_and_whitespace_queries() -> None:
    for empty_query in ("", "   ", "\t\n", None):
        result = classify_query(empty_query)  # type: ignore[arg-type]
        assert result["suggested_source"] == SOURCE_MEMORY
        assert result["confidence"] == CONFIDENCE_LOW
        assert result["reason"] == "empty_query"


def test_priority_when_two_sources_match() -> None:
    # code_graph vs git -> code_graph wins with low confidence
    code_vs_git = classify_query("Кто изменил класс MemorySearchService?")
    assert code_vs_git["suggested_source"] == SOURCE_CODE_GRAPH
    assert code_vs_git["confidence"] == CONFIDENCE_LOW
    assert code_vs_git["reason"] == "multi_source_conflict"
    assert code_vs_git["tools"] == CODE_GRAPH_TOOLS

    # code_graph vs docs -> code_graph wins with low confidence
    code_vs_docs = classify_query("Архитектура описанная в документации")
    assert code_vs_docs["suggested_source"] == SOURCE_CODE_GRAPH
    assert code_vs_docs["confidence"] == CONFIDENCE_LOW
    assert code_vs_docs["reason"] == "multi_source_conflict"
    assert code_vs_docs["tools"] == CODE_GRAPH_TOOLS

    # git vs docs -> git wins with low confidence
    git_vs_docs = classify_query("История изменений в README")
    assert git_vs_docs["suggested_source"] == SOURCE_GIT
    assert git_vs_docs["confidence"] == CONFIDENCE_LOW
    assert git_vs_docs["reason"] == "multi_source_conflict"
    assert git_vs_docs["tools"] == GIT_TOOLS

    # All three -> code_graph wins with low confidence
    all_three = classify_query("Кто изменил архитектуру в README?")
    assert all_three["suggested_source"] == SOURCE_CODE_GRAPH
    assert all_three["confidence"] == CONFIDENCE_LOW
    assert all_three["reason"] == "multi_source_conflict"


def test_finalize_search_response_code_graph() -> None:
    response: dict[str, Any] = {"memories": [], "total": 0, "query": "структура проекта"}
    request = SimpleNamespace(query="структура проекта", detail="full")
    finalized = finalize_search_response(response, request)

    assert finalized["suggested_source"] == SOURCE_CODE_GRAPH
    assert "routing" in finalized
    routing = finalized["routing"]
    assert routing["intent"] == "code_structure"
    assert routing["confidence"] == CONFIDENCE_HIGH
    assert routing["tools"] == CODE_GRAPH_TOOLS
    assert "структур" in routing["hint"] or "bhm_get_architecture" in routing["hint"]


def test_finalize_search_response_git() -> None:
    response: dict[str, Any] = {"memories": [], "total": 0, "query": "последний коммит"}
    request = SimpleNamespace(query="последний коммит", detail="full")
    finalized = finalize_search_response(response, request)

    assert finalized["suggested_source"] == SOURCE_GIT
    assert "routing" in finalized
    assert finalized["routing"]["tools"] == GIT_TOOLS


def test_finalize_search_response_docs() -> None:
    response: dict[str, Any] = {"memories": [], "total": 0, "query": "документация по API"}
    request = SimpleNamespace(query="документация по API", detail="full")
    finalized = finalize_search_response(response, request)

    assert finalized["suggested_source"] == SOURCE_DOCS
    assert "routing" in finalized
    assert finalized["routing"]["tools"] == DOCS_TOOLS


def test_finalize_search_response_memory_does_not_contain_routing() -> None:
    for mem_q in (
        "Как правильно деплоить на Ubuntu?",
        "Как перезапустить API памяти?",
        "Почему smoke релиза падал?",
        "Что решили про подпись релизов?",
    ):
        response: dict[str, Any] = {"memories": [], "total": 0, "query": mem_q}
        request = SimpleNamespace(query=mem_q, detail="full")
        finalized = finalize_search_response(response, request)

        assert finalized["suggested_source"] == SOURCE_MEMORY
        assert "routing" not in finalized


def test_finalize_search_response_empty_query() -> None:
    response: dict[str, Any] = {"memories": [], "total": 0, "query": ""}
    request = SimpleNamespace(query="   ", detail="full")
    finalized = finalize_search_response(response, request)

    assert finalized["suggested_source"] == SOURCE_MEMORY
    assert "routing" not in finalized


def test_compact_projection_preserves_suggested_source_and_routing() -> None:
    # Non-memory: keeps both suggested_source and routing
    code_resp: dict[str, Any] = {"memories": [], "total": 0, "query": "архитектура"}
    request = SimpleNamespace(query="архитектура", detail="compact")
    finalized = finalize_search_response(code_resp, request)

    assert finalized["detail"] == "compact"
    assert finalized["suggested_source"] == SOURCE_CODE_GRAPH
    assert "routing" in finalized
    assert finalized["routing"]["tools"] == CODE_GRAPH_TOOLS

    # Memory: keeps suggested_source, does NOT have routing
    mem_resp: dict[str, Any] = {"memories": [], "total": 0, "query": "Как правильно деплоить на Ubuntu?"}
    mem_req = SimpleNamespace(query="Как правильно деплоить на Ubuntu?", detail="compact")
    mem_finalized = finalize_search_response(mem_resp, mem_req)

    assert mem_finalized["detail"] == "compact"
    assert mem_finalized["suggested_source"] == SOURCE_MEMORY
    assert "routing" not in mem_finalized


def test_compact_search_response_direct_preserves_routing() -> None:
    full_resp: dict[str, Any] = {
        "total": 1,
        "limit": 10,
        "offset": 0,
        "query": "архитектура",
        "suggested_source": SOURCE_CODE_GRAPH,
        "routing": {
            "intent": "code_structure",
            "confidence": CONFIDENCE_HIGH,
            "reason": "code_graph_match",
            "tools": CODE_GRAPH_TOOLS,
            "hint": "hint text",
        },
        "memories": [],
    }
    compact = compact_search_response(full_resp)
    assert compact["suggested_source"] == SOURCE_CODE_GRAPH
    assert compact["routing"]["tools"] == CODE_GRAPH_TOOLS


def test_existing_keys_are_not_overwritten() -> None:
    response: dict[str, Any] = {
        "memories": [{"id": "m1"}],
        "total": 1,
        "suggested_source": "custom_source",
        "routing": {"custom": True},
    }
    request = SimpleNamespace(query="архитектура", detail="full")
    finalized = finalize_search_response(response, request)

    assert finalized["suggested_source"] == "custom_source"
    assert finalized["routing"] == {"custom": True}
    assert finalized["memories"] == [{"id": "m1"}]


def test_memory_search_service_integration() -> None:
    async def fake_ready():
        return None

    async def fake_federated(query, project, **kwargs):
        return ([{"id": "m1"}], 1)

    async def fake_advanced(request):
        return {"advanced": True}

    service = MemorySearchService(
        MemorySearchDependencies(
            ensure_provider_warmup_ready=fake_ready,
            effective_search_project=lambda p: p or "demo",
            federated_search=fake_federated,
            advanced_search=fake_advanced,
            serialize_vector_hit=lambda item: {"id": item["id"]},
            emit_memory_pulses=lambda items: None,
            is_fallback_grace_error=lambda exc: False,
            fallback_grace_memories_response=lambda route, reason, **kwargs: {"memories": []},
            local_collection_name=lambda project: f"local-{project}",
            global_collection_name=lambda: "global",
        )
    )

    # 1. Code graph query via MemorySearchService
    code_req = SimpleNamespace(
        query="какая структура проекта",
        project="demo",
        limit=5,
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
    result = asyncio.run(service.execute(code_req))
    assert result["suggested_source"] == SOURCE_CODE_GRAPH
    assert "routing" in result
    assert result["routing"]["tools"] == CODE_GRAPH_TOOLS

    # 2. Memory query via MemorySearchService
    mem_req = SimpleNamespace(
        query="Как правильно деплоить на Ubuntu?",
        project="demo",
        limit=5,
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
    mem_result = asyncio.run(service.execute(mem_req))
    assert mem_result["suggested_source"] == SOURCE_MEMORY
    assert "routing" not in mem_result
