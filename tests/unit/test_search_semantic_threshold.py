from __future__ import annotations

from types import SimpleNamespace

from blackholememory import app_core_hit


class _FakeMemory:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search(self, query: str, *, top_k: int, filters: dict, threshold: float = 0.1) -> dict:
        self.calls.append({"query": query, "top_k": top_k, "filters": filters, "threshold": threshold})
        return {"results": [{"id": query, "content": query, "score": 0.5, "metadata": {}}]}


def test_configured_threshold_reaches_local_and_global_memory_search(monkeypatch) -> None:
    local = _FakeMemory()
    global_memory = _FakeMemory()
    monkeypatch.setenv("BHM_SEARCH_SEMANTIC_THRESHOLD", "0.0")
    monkeypatch.setattr(app_core_hit, "local_collection_name", lambda _project: "legacy-local")
    monkeypatch.setattr(app_core_hit, "global_collection_name", lambda: "global")
    monkeypatch.setattr(app_core_hit, "get_project_mem0_memory", lambda *_args, **_kwargs: local)
    monkeypatch.setattr(app_core_hit, "get_global_core_memory", lambda: global_memory)
    monkeypatch.setattr(
        app_core_hit,
        "resolve_runtime_storage_config",
        lambda **_kwargs: SimpleNamespace(database_path="db"),
    )
    monkeypatch.setattr(
        app_core_hit,
        "resolve_projection_route",
        lambda **_kwargs: SimpleNamespace(collection_name="local", reason=""),
    )

    app_core_hit._search_memory_collection(
        query="local query",
        project_name="demo",
        context_origin=app_core_hit._VECTOR_CONTEXT_LOCAL,
        limit=5,
        query_embedding=[1.0],
    )
    app_core_hit._search_memory_collection(
        query="global query",
        project_name="demo",
        context_origin=app_core_hit._VECTOR_CONTEXT_GLOBAL,
        limit=5,
        query_embedding=[1.0],
    )

    assert local.calls[0]["threshold"] == 0.0
    assert global_memory.calls[0]["threshold"] == 0.0
