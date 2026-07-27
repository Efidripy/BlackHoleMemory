from __future__ import annotations

from blackholememory.embedding_reuse import embed_query_once
from blackholememory.embedding_reuse import search_with_precomputed_embedding


class _FakeEmbedder:
    def __init__(self):
        self.calls: list[str] = []

    def embed(self, text: str, *_args, **_kwargs):
        self.calls.append(text)
        return [float(len(text))]


class _FakeMemory:
    def __init__(self):
        self.embedding_model = _FakeEmbedder()
        self.search_calls: list[dict] = []

    def search(self, query: str, *, top_k: int, filters: dict):
        vector = self.embedding_model.embed(query, "search")
        self.search_calls.append({"query": query, "top_k": top_k, "filters": filters, "vector": vector})
        return {"results": [{"vector": vector}]}


def test_embed_query_once_uses_provider_once():
    memory = _FakeMemory()

    assert embed_query_once(memory, "same query") == [10.0]
    assert memory.embedding_model.calls == ["same query"]


def test_precomputed_search_preserves_mem0_search_contract_without_reembedding_query():
    memory = _FakeMemory()
    result = search_with_precomputed_embedding(
        memory,
        "same query",
        [42.0],
        top_k=20,
        filters={"user_id": "user-1"},
    )

    assert result == {"results": [{"vector": [42.0]}]}
    assert memory.embedding_model.calls == []
