"""Application service for the canonical project-scoped memory search path.

The service owns the response/fallback policy for ``POST /bhm/search`` while
the HTTP adapter remains responsible for request models and dependency wiring.
MCP continues to call the same REST path, so the transport and public contract
remain unchanged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .retrieval_query_plan import build_retrieval_query_plan


def read_only_side_effects() -> dict[str, Any]:
    """Describe the storage contract for retrieval-only responses.

    Retrieval may emit bounded in-process usefulness pulses, but it must not
    write the SQLite authority or the rebuildable Qdrant projection.  Keep the
    value as a factory so callers cannot accidentally share mutable response
    state.
    """

    return {
        "read_only": True,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "projection_mutation": False,
    }


@dataclass(frozen=True)
class MemorySearchDependencies:
    """Runtime callbacks kept at the adapter boundary for parity and tests."""

    ensure_provider_warmup_ready: Callable[[], Awaitable[None]]
    effective_search_project: Callable[[str | None], str]
    federated_search: Callable[..., Awaitable[tuple[list[dict], int]]]
    advanced_search: Callable[[Any], Awaitable[dict[str, Any]]]
    serialize_vector_hit: Callable[[dict], dict]
    emit_memory_pulses: Callable[[list[dict]], None]
    is_fallback_grace_error: Callable[[Exception], bool]
    fallback_grace_memories_response: Callable[..., dict[str, Any]]
    local_collection_name: Callable[[str], str]
    global_collection_name: Callable[[], str]


class MemorySearchService:
    """Execute the canonical federated memory-search use case."""

    def __init__(self, dependencies: MemorySearchDependencies) -> None:
        self._dependencies = dependencies

    async def execute(self, request: Any) -> dict[str, Any]:
        """Return a stable search response without changing storage state."""

        if not request.query.strip():
            response = await self._dependencies.advanced_search(request)
            response.setdefault("side_effects", read_only_side_effects())
            return response

        # Typed/temporal filters are correctness-first queries.  Until the
        # typed Qdrant payload parity gate is enabled, answer them directly
        # from the SQLite-authoritative advanced path.  This avoids sending a
        # wide legacy projection through provider/graph retrieval and keeps a
        # slow local embedding model from taking the API with it.
        typed_or_temporal = any(
            getattr(request, field, None) is not None
            for field in ("memory_class", "event_role", "as_of", "valid_from", "valid_to")
        ) or bool(getattr(request, "include_temporal_unknown", False))
        if typed_or_temporal:
            response = await self._dependencies.advanced_search(request)
            response.setdefault("retrieval", {})
            response["retrieval"].update(
                {
                    "mode": "authoritative-typed",
                    "provider": "sqlite",
                    "semantic_projection_used": False,
                }
            )
            response.setdefault("side_effects", read_only_side_effects())
            return response

        project_name = self._dependencies.effective_search_project(request.project)
        try:
            await self._dependencies.ensure_provider_warmup_ready()
            started = time.perf_counter()
            federated_result = await self._dependencies.federated_search(
                request.query,
                project_name,
                limit=request.limit,
                offset=request.offset,
                memory_type=request.memory_type,
                memory_class=getattr(request, "memory_class", None),
                event_role=getattr(request, "event_role", None),
                as_of=getattr(request, "as_of", None),
                valid_from=getattr(request, "valid_from", None),
                valid_to=getattr(request, "valid_to", None),
                include_temporal_unknown=getattr(request, "include_temporal_unknown", False),
                concepts=request.concepts,
                files=request.files,
                domain=request.domain,
                semantic_type=request.semantic_type,
                priority=request.priority,
                include_archived=request.include_archived,
                include_logs=request.include_logs,
                include_historical=getattr(request, "include_historical", False),
            )
            hits, total = federated_result
            query_plan = build_retrieval_query_plan(
                requested_limit=request.limit,
                offset=request.offset,
                total_candidates=total,
                returned_hits=hits,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                include_global=True,
                include_graph_expansion=True,
                typed_filter_requested=(
                    getattr(request, "memory_class", None) is not None
                    or getattr(request, "event_role", None) is not None
                ),
                temporal_filter_requested=any(
                    getattr(request, field, None) is not None
                    for field in ("as_of", "valid_from", "valid_to")
                ) or bool(getattr(request, "include_temporal_unknown", False)),
                contour_trace=getattr(federated_result, "contour_trace", None),
            )
            memories = [self._dependencies.serialize_vector_hit(item) for item in hits]
            self._dependencies.emit_memory_pulses(memories)
            if total == 0:
                response = await self._dependencies.advanced_search(request)
                response.setdefault("side_effects", read_only_side_effects())
                response["retrieval"] = {
                    "mode": "federated-empty-live-fallback",
                    "local_collection": self._dependencies.local_collection_name(project_name),
                    "global_collection": self._dependencies.global_collection_name(),
                }
                return response
            return {
                "memories": memories,
                "total": total,
                "limit": max(min(request.limit, 200), 1),
                "offset": max(request.offset, 0),
                "query": request.query,
                "filters": {
                    "project": request.project,
                    "memory_type": request.memory_type,
                    "memory_class": (
                        getattr(request.memory_class, "value", None)
                        if getattr(getattr(request, "memory_class", None), "value", None)
                        else getattr(request, "memory_class", None)
                    ),
                    "event_role": (
                        getattr(request.event_role, "value", None)
                        if getattr(getattr(request, "event_role", None), "value", None)
                        else getattr(request, "event_role", None)
                    ),
                    "as_of": getattr(request, "as_of", None),
                    "valid_from": getattr(request, "valid_from", None),
                    "valid_to": getattr(request, "valid_to", None),
                    "include_temporal_unknown": getattr(request, "include_temporal_unknown", False),
                    "concepts": request.concepts or [],
                    "files": request.files or [],
                    "include_archived": request.include_archived,
                    "include_logs": request.include_logs,
                    "include_historical": getattr(request, "include_historical", False),
                    "domain": request.domain,
                    "semantic_type": request.semantic_type,
                    "priority": request.priority,
                },
                "retrieval": {
                    "mode": "federated",
                    "ranking": "rrf-hybrid",
                    "local_collection": self._dependencies.local_collection_name(project_name),
                    "global_collection": self._dependencies.global_collection_name(),
                    "query_plan": query_plan,
                },
                "side_effects": read_only_side_effects(),
            }
        except Exception as exc:
            if not self._dependencies.is_fallback_grace_error(exc):
                raise
            response = self._dependencies.fallback_grace_memories_response(
                "bhm.search.federated",
                exc,
                project=project_name,
                memory_type=request.memory_type,
                memory_class=getattr(request, "memory_class", None),
                event_role=getattr(request, "event_role", None),
                as_of=getattr(request, "as_of", None),
                valid_from=getattr(request, "valid_from", None),
                valid_to=getattr(request, "valid_to", None),
                include_temporal_unknown=getattr(request, "include_temporal_unknown", False),
                concepts=request.concepts,
                files=request.files,
                query=request.query,
                include_logs=request.include_logs,
                include_historical=getattr(request, "include_historical", False),
                domain=request.domain,
                semantic_type=request.semantic_type,
                priority=request.priority,
                include_archived=request.include_archived,
                limit=request.limit,
                offset=request.offset,
            )
            response["query"] = request.query
            response["retrieval"] = {
                "mode": "federated-fallback-grace",
                "local_collection": self._dependencies.local_collection_name(project_name),
                "global_collection": self._dependencies.global_collection_name(),
            }
            response.setdefault("side_effects", read_only_side_effects())
            return response
