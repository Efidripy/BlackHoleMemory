from __future__ import annotations

from blackholememory.embedding_cache import EmbeddingCache
from blackholememory.embedding_cache import embed_queries_with_cache
from blackholememory.embedding_cache import embed_query_with_cache


def test_embedding_cache_is_bounded_lru_and_ttl_aware():
    now = [0.0]
    cache = EmbeddingCache(max_entries=2, ttl_seconds=10, clock=lambda: now[0])
    cache.put("a", [1.0])
    cache.put("b", [2.0])
    assert cache.get("a") == [1.0]
    cache.put("c", [3.0])
    assert cache.get("b") is None
    now[0] = 11.0
    assert cache.get("a") is None


def test_embedding_cache_reuses_query_without_provider_call():
    class Embedder:
        calls = 0

        def embed(self, query, _action):
            self.calls += 1
            return [float(len(query))]

    embedder = Embedder()
    cache = EmbeddingCache()
    assert embed_query_with_cache(embedder, "same", model_key="m1", cache=cache) == ([4.0], False)
    assert embed_query_with_cache(embedder, "same", model_key="m1", cache=cache) == ([4.0], True)
    assert embedder.calls == 1


def test_embedding_batch_deduplicates_and_uses_provider_batch_method():
    class Embedder:
        def __init__(self):
            self.batch_calls = 0

        def embed_batch(self, queries, memory_action):
            self.batch_calls += 1
            return [[float(len(query))] for query in queries]

    embedder = Embedder()
    result = embed_queries_with_cache(
        embedder,
        ["one", "two", "one"],
        model_key="m1",
        cache=EmbeddingCache(),
    )

    assert result.vectors == ([3.0], [3.0], [3.0])
    assert result.unique_queries == 2
    assert result.provider_calls == 1
    assert result.cache_hits == 0
    assert embedder.batch_calls == 1


def test_embedding_batch_falls_back_when_provider_has_no_batch_method():
    class Embedder:
        def __init__(self):
            self.calls = 0

        def embed(self, query, _action):
            self.calls += 1
            return [float(len(query))]

    embedder = Embedder()
    cache = EmbeddingCache()
    first = embed_queries_with_cache(embedder, ["one", "two"], model_key="m1", cache=cache)
    second = embed_queries_with_cache(embedder, ["one", "two"], model_key="m1", cache=cache)

    assert first.provider_calls == 2
    assert second.provider_calls == 0
    assert second.cache_hits == 2
    assert embedder.calls == 2
