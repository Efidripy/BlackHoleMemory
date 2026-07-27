"""Bounded query-embedding cache and provider-aware batching helpers."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable


def embedding_cache_key(query: str, model_key: str) -> str:
    digest = hashlib.sha256(str(query).encode("utf-8")).hexdigest()
    return f"{model_key}:{digest}"


@dataclass(frozen=True)
class BatchEmbeddingResult:
    vectors: tuple[Any, ...]
    cache_hits: int
    provider_calls: int
    unique_queries: int


class EmbeddingCache:
    def __init__(
        self,
        *,
        max_entries: int = 256,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_entries = max(int(max_entries), 1)
        self.ttl_seconds = max(float(ttl_seconds), 0.1)
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def get(self, key: str) -> Any | None:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return value

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._entries[key] = (self._clock() + self.ttl_seconds, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def get_or_compute(self, key: str, compute: Callable[[], Any]) -> tuple[Any, bool]:
        cached = self.get(key)
        if cached is not None:
            return cached, True
        value = compute()
        self.put(key, value)
        return value, False

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._entries), "max_entries": self.max_entries}


def embed_query_with_cache(
    embedder: Any,
    query: str,
    *,
    model_key: str,
    cache: EmbeddingCache,
) -> tuple[Any, bool]:
    key = embedding_cache_key(query, model_key)
    return cache.get_or_compute(key, lambda: embedder.embed(query, "search"))


def embed_queries_with_cache(
    embedder: Any,
    queries: list[str] | tuple[str, ...],
    *,
    model_key: str,
    cache: EmbeddingCache,
) -> BatchEmbeddingResult:
    """Embed unique queries with provider batching where available."""

    ordered_queries = list(queries)
    unique_queries = list(dict.fromkeys(query for query in ordered_queries if str(query).strip()))
    vectors_by_query: dict[str, Any] = {}
    missing: list[str] = []
    cache_hits = 0
    for query in unique_queries:
        cached = cache.get(embedding_cache_key(query, model_key))
        if cached is None:
            missing.append(query)
        else:
            vectors_by_query[query] = cached
            cache_hits += 1

    provider_calls = 0
    if missing:
        embed_batch = getattr(embedder, "embed_batch", None)
        if callable(embed_batch):
            computed = embed_batch(missing, memory_action="search")
            provider_calls = 1
        else:
            computed = [embedder.embed(query, "search") for query in missing]
            provider_calls = len(missing)
        if len(computed) != len(missing):
            raise ValueError("embedding provider returned an unexpected batch length")
        for query, vector in zip(missing, computed, strict=True):
            cache.put(embedding_cache_key(query, model_key), vector)
            vectors_by_query[query] = vector

    return BatchEmbeddingResult(
        vectors=tuple(vectors_by_query[query] for query in ordered_queries if str(query).strip()),
        cache_hits=cache_hits,
        provider_calls=provider_calls,
        unique_queries=len(unique_queries),
    )
