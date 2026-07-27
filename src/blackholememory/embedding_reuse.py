"""Reuse one query embedding across independent Mem0 search contours."""

from __future__ import annotations

from copy import copy
from typing import Any


class _PrecomputedEmbeddingModel:
    def __init__(self, original: Any, query: str, embedding: Any):
        self._original = original
        self._query = query
        self._embedding = embedding
        self._used = False

    def embed(self, text: str, *args: Any, **kwargs: Any) -> Any:
        if not self._used and text == self._query:
            self._used = True
            return self._embedding
        return self._original.embed(text, *args, **kwargs)


def embed_query_once(memory: Any, query: str) -> Any:
    """Compute the provider embedding exactly once for a federated query."""

    return memory.embedding_model.embed(query, "search")


def search_with_precomputed_embedding(
    memory: Any,
    query: str,
    embedding: Any,
    *,
    top_k: int,
    filters: dict[str, Any],
) -> Any:
    """Call Mem0's normal search path while injecting one already-made vector.

    A shallow clone isolates the temporary embedding adapter from the cached
    process-wide Memory object and preserves Mem0's filter normalization,
    vector/BM25 scoring and reranking behavior.
    """

    isolated_memory = copy(memory)
    isolated_memory.embedding_model = _PrecomputedEmbeddingModel(
        memory.embedding_model,
        query,
        embedding,
    )
    return isolated_memory.search(query, top_k=top_k, filters=filters)
