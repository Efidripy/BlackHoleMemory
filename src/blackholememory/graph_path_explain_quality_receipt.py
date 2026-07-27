"""Bounded quality receipt for graph explain paths.

This module is deliberately additive to the existing graph query/explain
surface.  It summarizes path completeness and provenance quality without
returning source, raw edge evidence or any mutable state.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from typing import Any


GRAPH_PATH_EXPLAIN_QUALITY_RECEIPT_SCHEMA_VERSION = "bhm.code-graph.path-explain-quality-receipt.v1"
_MAX_PATHS = 128
_MAX_HOPS = 8


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _bounded_text(value: Any, limit: int = 160) -> str:
    return str(value or "")[:limit]


def _classify_path(item: Mapping[str, Any], *, response_truncated: bool) -> tuple[str, int, int, int, bool, int, float]:
    receipt = item.get("path_receipt") if isinstance(item.get("path_receipt"), Mapping) else {}
    provenance = receipt.get("edge_provenance") if isinstance(receipt.get("edge_provenance"), list) else []
    safe_provenance = [edge for edge in provenance if isinstance(edge, Mapping)]
    hops = max(0, min(int(receipt.get("hops") or len(safe_provenance)), _MAX_HOPS))
    edge_count = len(safe_provenance)
    unresolved_count = sum(bool(edge.get("unresolved")) for edge in safe_provenance)
    provenance_complete = bool(edge_count == hops and all(str(edge.get("provenance_digest") or "") for edge in safe_provenance))
    reason = _bounded_text(item.get("reason"), 48).casefold()
    source_ref_count = len({str(ref) for ref in (item.get("source_refs") or []) if str(ref)}) if isinstance(item.get("source_refs"), list) else 0
    cost = max(0.0, float(receipt.get("cost") or 0.0))
    if reason == "seed_match" and hops == 0:
        classification = "seed"
    elif unresolved_count:
        classification = "unresolved"
    elif response_truncated or not provenance_complete:
        classification = "partial"
    elif hops > 0:
        classification = "complete"
    else:
        classification = "unproven"
    return classification, hops, edge_count, unresolved_count, provenance_complete, source_ref_count, round(cost, 6)


def build_graph_path_explain_quality_receipt(response: Mapping[str, Any]) -> dict[str, Any]:
    """Build a deterministic, metadata-only path quality receipt."""

    explanations = response.get("explanations") if isinstance(response.get("explanations"), list) else []
    bounds = response.get("bounds") if isinstance(response.get("bounds"), Mapping) else {}
    plan = response.get("query_plan") if isinstance(response.get("query_plan"), Mapping) else {}
    response_truncated = bool(bounds.get("truncated") or bounds.get("budget_exceeded") or response.get("stale"))
    rows: list[dict[str, Any]] = []
    classes: Counter[str] = Counter()
    max_hops = 0
    for item in explanations[:_MAX_PATHS]:
        if not isinstance(item, Mapping):
            continue
        classification, hops, edge_count, unresolved_count, provenance_complete, source_ref_count, cost = _classify_path(
            item,
            response_truncated=response_truncated,
        )
        classes[classification] += 1
        max_hops = max(max_hops, hops)
        rows.append(
            {
                "node_id": _bounded_text(item.get("node_id"), 160),
                "stable_key": _bounded_text(item.get("stable_key"), 240),
                "reason": _bounded_text(item.get("reason"), 48),
                "classification": classification,
                "hops": hops,
                "edge_count": edge_count,
                "unresolved_edge_count": unresolved_count,
                "provenance_complete": provenance_complete,
                "source_ref_count": min(source_ref_count, 32),
                "cost": cost,
            }
        )
    explained = len(rows)
    path_rows = [row for row in rows if row["classification"] not in {"seed", "unproven"}]
    complete_paths = sum(row["classification"] == "complete" for row in path_rows)
    unresolved_paths = sum(row["classification"] == "unresolved" for row in path_rows)
    partial_paths = sum(row["classification"] == "partial" for row in path_rows)
    path_count = len(path_rows)
    if response_truncated or unresolved_paths or partial_paths:
        status = "review_required"
    elif explained == 0:
        status = "not_observed"
    else:
        status = "complete"
    core = {
        "schema_version": GRAPH_PATH_EXPLAIN_QUALITY_RECEIPT_SCHEMA_VERSION,
        "status": status,
        "graph_binding": {
            "snapshot_id": _bounded_text(response.get("snapshot_id"), 128),
            "graph_digest": _bounded_text(response.get("graph_digest"), 128),
            "bound": bool(str(response.get("snapshot_id") or "").strip() and str(response.get("graph_digest") or "").strip()),
        },
        "query": {
            "operation": _bounded_text(response.get("operation"), 64),
            "explain": bool(str(response.get("schema_version") or "").endswith("explain.v1")),
            "allowlisted": bool(plan.get("allowlisted")),
            "read_only": bool(plan.get("read_only")),
            "arbitrary_sql": bool(plan.get("arbitrary_sql")),
        },
        "path_coverage": {
            "explained_count": explained,
            "path_count": path_count,
            "complete_path_count": complete_paths,
            "partial_path_count": partial_paths,
            "unresolved_path_count": unresolved_paths,
            "seed_count": int(classes.get("seed", 0)),
            "unproven_count": int(classes.get("unproven", 0)),
            "complete_ratio": round(complete_paths / path_count, 6) if path_count else 0.0,
            "max_observed_hops": max_hops,
            "max_hops_bound": _MAX_HOPS,
            "path_bucket": "complete" if path_count and complete_paths == path_count else ("partial" if complete_paths else "not_observed"),
        },
        "classification_counts": {key: int(value) for key, value in sorted(classes.items())},
        "paths": rows,
        "bounds": {
            "truncated": bool(bounds.get("truncated")),
            "budget_exceeded": bool(bounds.get("budget_exceeded")),
            "stale_snapshot": bool(response.get("stale")),
            "max_tokens": max(0, min(int(bounds.get("max_tokens") or 0), 16_384)),
            "time_budget_ms": max(0.0, min(float(bounds.get("time_budget_ms") or 0.0), 5_000.0)),
        },
        "provenance": {
            "authority": "sqlite-authoritative-code-graph",
            "source_free": True,
            "raw_edge_evidence_returned": False,
            "review_required": status != "complete",
        },
        "execution": {
            "proposal_only": True,
            "read_only": True,
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "writes_retrieval": False,
            "network": False,
            "model_started": False,
            "raw_source_returned": False,
            "edge_promotion": False,
        },
    }
    return {**core, "evidence_digest": _digest(core)}


__all__ = [
    "GRAPH_PATH_EXPLAIN_QUALITY_RECEIPT_SCHEMA_VERSION",
    "build_graph_path_explain_quality_receipt",
]
