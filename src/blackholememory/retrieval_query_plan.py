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
_ALLOWED_CONTOUR_NAMES = frozenset({"local_vector", "global_vector", "exact_identifier"})
_ALLOWED_CONTOUR_STATUS = frozenset({"completed", "failed", "timed_out", "disabled"})


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


def _bounded_duration_ms(value: object) -> float:
    try:
        return round(max(0.0, min(float(value), 60_000.0)), 3)
    except (TypeError, ValueError):
        return 0.0


def _contour_timing_stage(trace: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Sanitize an in-process trace; never reflect raw caller-controlled fields."""

    if not isinstance(trace, Mapping) or trace.get("schema_version") != "bhm.retrieval-contour-trace.v1":
        return None
    raw_embedding = trace.get("embedding") if isinstance(trace.get("embedding"), Mapping) else {}
    contours: list[dict[str, Any]] = []
    raw_contours = trace.get("contours") if isinstance(trace.get("contours"), list) else []
    for raw_contour in raw_contours:
        if not isinstance(raw_contour, Mapping):
            continue
        name = str(raw_contour.get("name") or "")
        status = str(raw_contour.get("status") or "")
        if name not in _ALLOWED_CONTOUR_NAMES or status not in _ALLOWED_CONTOUR_STATUS:
            continue
        contours.append(
            {
                "name": name,
                "enabled": bool(raw_contour.get("enabled")),
                "status": status,
                "duration_ms": _bounded_duration_ms(raw_contour.get("duration_ms")),
                "deadline_ms": _bounded_duration_ms(raw_contour.get("deadline_ms")),
            }
        )
    return {
        "name": "contour_timings",
        "schema_version": "bhm.retrieval-contour-trace.v1",
        "total_duration_ms": _bounded_duration_ms(trace.get("total_duration_ms")),
        "embedding": {
            "enabled": bool(raw_embedding.get("enabled")),
            "status": "completed" if bool(raw_embedding.get("enabled")) else "skipped",
            "duration_ms": _bounded_duration_ms(raw_embedding.get("duration_ms")),
        },
        "contours": contours,
    }


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
    contour_trace: Mapping[str, Any] | None = None,
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
    stages = [
        {"name": "identity_scope", "status": "project_bound"},
        {
            "name": "metadata_prefilter",
            "typed_filter_requested": bool(typed_filter_requested),
            "temporal_filter_requested": bool(temporal_filter_requested),
        },
        {"name": "candidate_routes", "returned_by_route": routes, "global_enabled": bool(include_global)},
    ]
    if timing_stage := _contour_timing_stage(contour_trace):
        stages.append(timing_stage)
    stages.extend([
        {"name": "fusion", "strategy": "existing-deterministic-ranker"},
        {"name": "post_filter", "graph_expansion_enabled": bool(include_graph_expansion)},
    ])
    return {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "requested_limit": requested,
        "offset": safe_offset,
        "total_candidates": total,
        "returned_candidates": returned,
        "underfill_reason": underfill_reason,
        "duration_ms": _bounded_duration_ms(duration_ms),
        "stages": stages,
        "query_plan_builder": {
            "reads_completed_search_result_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "retrieval_policy_changed": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "build_retrieval_query_plan"]
