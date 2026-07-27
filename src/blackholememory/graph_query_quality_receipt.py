"""Bounded quality/coverage receipt for the existing graph query surface."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from typing import Any


GRAPH_QUERY_QUALITY_RECEIPT_SCHEMA_VERSION = "bhm.code-graph.query-quality-receipt.v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _ratio_bucket(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "not_observed"
    ratio = max(0.0, min(1.0, float(numerator) / float(denominator)))
    if ratio >= 1.0:
        return "complete"
    if ratio >= 0.75:
        return "high"
    if ratio >= 0.25:
        return "partial"
    return "low"


def build_graph_query_quality_receipt(response: Mapping[str, Any]) -> dict[str, Any]:
    """Build a deterministic, source-free quality receipt from one query response."""

    nodes = response.get("nodes") if isinstance(response.get("nodes"), list) else []
    edges = response.get("edges") if isinstance(response.get("edges"), list) else []
    plan = response.get("query_plan") if isinstance(response.get("query_plan"), Mapping) else {}
    bounds = response.get("bounds") if isinstance(response.get("bounds"), Mapping) else {}
    pagination = response.get("pagination") if isinstance(response.get("pagination"), Mapping) else {}
    candidate_nodes = max(0, min(int(plan.get("candidate_node_count") or len(nodes)), 100_000))
    candidate_edges = max(0, min(int(plan.get("candidate_edge_count") or len(edges)), 300_000))
    result_nodes = min(len(nodes), 100_000)
    result_edges = min(len(edges), 300_000)
    truncated = bool(bounds.get("truncated"))
    budget_exceeded = bool(bounds.get("budget_exceeded"))
    stale = bool(response.get("stale"))
    status = "complete" if not truncated and not budget_exceeded and not stale else "partial"
    node_kinds = Counter(str(node.get("node_kind") or "unknown")[:80] for node in nodes if isinstance(node, Mapping))
    edge_kinds = Counter(str(edge.get("edge_kind") or "unknown")[:80] for edge in edges if isinstance(edge, Mapping))
    unresolved_count = sum(bool(edge.get("unresolved")) for edge in edges if isinstance(edge, Mapping))
    core = {
        "schema_version": GRAPH_QUERY_QUALITY_RECEIPT_SCHEMA_VERSION,
        "status": status,
        "graph_binding": {
            "snapshot_id": str(response.get("snapshot_id") or "")[:128],
            "graph_digest": str(response.get("graph_digest") or "")[:128],
            "bound": bool(str(response.get("snapshot_id") or "").strip() and str(response.get("graph_digest") or "").strip()),
        },
        "query": {
            "operation": str(response.get("operation") or "")[:64],
            "allowlisted": bool(plan.get("allowlisted")),
            "read_only": bool(plan.get("read_only")),
            "arbitrary_sql": bool(plan.get("arbitrary_sql")),
        },
        "coverage": {
            "candidate_node_count": candidate_nodes,
            "result_node_count": result_nodes,
            "candidate_edge_count": candidate_edges,
            "result_edge_count": result_edges,
            "node_bucket": _ratio_bucket(result_nodes, candidate_nodes),
            "edge_bucket": _ratio_bucket(result_edges, candidate_edges),
            "pagination_total_seed_count": max(0, min(int(pagination.get("total_seed_count") or 0), 100_000)),
        },
        "histograms": {
            "node_kinds": {key: int(value) for key, value in sorted(node_kinds.items())},
            "edge_kinds": {key: int(value) for key, value in sorted(edge_kinds.items())},
            "unresolved_edge_count": max(0, min(unresolved_count, 300_000)),
        },
        "bounds": {
            "truncated": truncated,
            "budget_exceeded": budget_exceeded,
            "stale_snapshot": stale,
            "max_tokens": max(0, min(int(bounds.get("max_tokens") or 0), 16_384)),
            "time_budget_ms": max(0.0, min(float(bounds.get("time_budget_ms") or 0.0), 5_000.0)),
        },
        "provenance": {"authority": "sqlite-authoritative-code-graph", "source_free": True, "review_required": status != "complete"},
        "execution": {"proposal_only": True, "read_only": True, "writes_sqlite_state": False, "writes_qdrant": False, "writes_retrieval": False, "network": False, "raw_source_returned": False, "model_started": False},
    }
    return {**core, "evidence_digest": _digest(core)}


__all__ = ["GRAPH_QUERY_QUALITY_RECEIPT_SCHEMA_VERSION", "build_graph_query_quality_receipt"]
