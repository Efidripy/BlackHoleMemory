"""Bounded semantic edge taxonomy receipt for graph query/explain responses."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from typing import Any


GRAPH_EDGE_TAXONOMY_RECEIPT_SCHEMA_VERSION = "bhm.code-graph.edge-taxonomy-receipt.v1"
KNOWN_EDGE_KINDS = frozenset(
    {
        "calls", "async_calls", "imports", "tests", "inherits", "route_handles",
        "http_calls", "emits", "listens_on", "data_flows", "similar_to",
        "depends_on", "exposes", "contains",
    }
)
EDGE_FAMILY_BY_KIND = {
    "contains": "containment",
    "imports": "dependency",
    "inherits": "dependency",
    "depends_on": "dependency",
    "calls": "call_control",
    "async_calls": "call_control",
    "route_handles": "service",
    "http_calls": "service",
    "emits": "service",
    "listens_on": "service",
    "exposes": "service",
    "data_flows": "data_flow",
    "tests": "test",
    "similar_to": "semantic",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _confidence_bucket(value: Any) -> str:
    try:
        confidence = float(value or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


def build_graph_edge_taxonomy_receipt(response: Mapping[str, Any]) -> dict[str, Any]:
    """Classify bounded edge views without exposing edge attributes or source."""

    edges = response.get("edges") if isinstance(response.get("edges"), list) else []
    kind_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()
    unresolved_count = 0
    for edge in edges:
        if not isinstance(edge, Mapping):
            continue
        kind = str(edge.get("edge_kind") or "unknown").strip().casefold()[:80] or "unknown"
        family = EDGE_FAMILY_BY_KIND.get(kind, "unknown")
        kind_counts[kind] += 1
        family_counts[family] += 1
        confidence_counts[_confidence_bucket(edge.get("confidence"))] += 1
        unresolved_count += int(bool(edge.get("unresolved")))
    observed_kinds = set(kind_counts)
    core = {
        "schema_version": GRAPH_EDGE_TAXONOMY_RECEIPT_SCHEMA_VERSION,
        "status": "complete" if edges and "unknown" not in family_counts else ("partial" if edges else "not_observed"),
        "graph_binding": {
            "snapshot_id": str(response.get("snapshot_id") or "")[:128],
            "graph_digest": str(response.get("graph_digest") or "")[:128],
            "bound": bool(str(response.get("snapshot_id") or "").strip() and str(response.get("graph_digest") or "").strip()),
        },
        "coverage": {
            "observed_edge_count": len(edges),
            "observed_kind_count": len(observed_kinds),
            "known_kind_count": len(observed_kinds & KNOWN_EDGE_KINDS),
            "allowlisted_kind_coverage": "complete" if observed_kinds and observed_kinds <= KNOWN_EDGE_KINDS else ("partial" if observed_kinds else "not_observed"),
        },
        "families": dict(sorted(family_counts.items())),
        "edge_kinds": dict(sorted(kind_counts.items())),
        "confidence": dict(sorted(confidence_counts.items())),
        "unresolved_edge_count": max(0, min(unresolved_count, 300_000)),
        "provenance": {"authority": "sqlite-authoritative-code-graph", "metadata_only": True, "review_required": bool("unknown" in observed_kinds or unresolved_count), "raw_attributes_returned": False},
        "execution": {"read_only": True, "writes_sqlite_state": False, "writes_qdrant": False, "network": False, "compiler_or_lsp": False, "arbitrary_sql": False, "edge_promotion": False, "raw_source_returned": False},
    }
    return {**core, "evidence_digest": _digest(core)}


__all__ = ["GRAPH_EDGE_TAXONOMY_RECEIPT_SCHEMA_VERSION", "build_graph_edge_taxonomy_receipt"]
