"""Content-free observability receipt for the canonical retrieval path.

The builder records only deterministic route/count/filter signals already
available to the caller.  It never receives a query, memory content, IDs,
paths or model scores and cannot alter retrieval policy.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "bhm.retrieval-query-plan.v1"
_ALLOWED_ORIGINS = frozenset({"LOCAL", "GLOBAL"})
_ALLOWED_ROUTES = frozenset({"vector", "exact-identifier"})


def _bounded_count(value: object, *, maximum: int = 200) -> int:
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError):
        return 0


def _route_counts(hits: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for hit in hits:
        metadata = hit.get("metadata") if isinstance(hit.get("metadata"), Mapping) else {}
        origin = str(hit.get("context_origin") or metadata.get("context_origin") or "LOCAL").upper()
        route = str(metadata.get("retrieval_route") or "vector").casefold()
        if origin not in _ALLOWED_ORIGINS:
            origin = "LOCAL"
        if route not in _ALLOWED_ROUTES:
            route = "vector"
        counts[f"{origin.lower()}:{route}"] += 1
    return dict(sorted(counts.items()))


def build_retrieval_query_plan(
    *,
    requested_limit: int,
    offset: int,
    total_candidates: int,
    returned_hits: Iterable[Mapping[str, Any]],
    duration_ms: float,
    include_global: bool,
    include_graph_expansion: bool,
    typed_filter_requested: bool,
    temporal_filter_requested: bool,
) -> dict[str, Any]:
    """Build a bounded plan receipt for one completed read-only search."""

    requested = _bounded_count(requested_limit)
    safe_offset = _bounded_count(offset)
    total = _bounded_count(total_candidates, maximum=100_000)
    routes = _route_counts(returned_hits)
    returned = min(sum(routes.values()), requested)
    underfill_reason: str | None = None
    if returned < requested:
        underfill_reason = "no_eligible_candidates" if total == 0 else "eligible_candidates_exhausted"
    return {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "requested_limit": requested,
        "offset": safe_offset,
        "total_candidates": total,
        "returned_candidates": returned,
        "underfill_reason": underfill_reason,
        "duration_ms": round(max(0.0, min(float(duration_ms), 60_000.0)), 3),
        "stages": [
            {"name": "identity_scope", "status": "project_bound"},
            {
                "name": "metadata_prefilter",
                "typed_filter_requested": bool(typed_filter_requested),
                "temporal_filter_requested": bool(temporal_filter_requested),
            },
            {"name": "candidate_routes", "returned_by_route": routes, "global_enabled": bool(include_global)},
            {"name": "fusion", "strategy": "existing-deterministic-ranker"},
            {"name": "post_filter", "graph_expansion_enabled": bool(include_graph_expansion)},
        ],
        "query_plan_builder": {
            "reads_completed_search_result_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "retrieval_policy_changed": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "build_retrieval_query_plan"]
