"""Bounded graph-bound cross-service trace/path receipts.

The receipt is derived only from an already published SQLite code-graph
snapshot.  It is intentionally a proposal surface: edge attributes are
represented by digests, no source is returned, and no graph edge is promoted.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, deque
from typing import Any, Mapping, Sequence


SERVICE_TRACE_RECEIPT_SCHEMA_VERSION = "bhm.cross-service-trace-receipt.v1"
PROTOCOL_ATTRIBUTION_SCHEMA_VERSION = "bhm.cross-service-protocol-receipt.v1"
PROTOCOL_FAMILIES = frozenset({"http", "grpc", "graphql", "trpc", "pubsub", "unknown"})
ALLOWED_SERVICE_TRACE_EDGE_KINDS = frozenset({"http_calls", "async_calls", "emits", "listens_on", "data_flows", "depends_on"})
MAX_TRACE_HOPS = 6
MAX_TRACE_PATHS = 128
MAX_TRACE_NEIGHBORS = 32


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _clip(value: Any, limit: int = 160) -> str:
    return str(value or "").strip()[:limit]


def _node_label(node: Mapping[str, Any]) -> dict[str, str]:
    return {
        "node_id": _clip(node.get("node_id"), 160),
        "node_kind": _clip(node.get("node_kind"), 80),
        "name": _clip(node.get("name") or node.get("qualified_name"), 160),
    }


def _protocol_family(edge: Mapping[str, Any]) -> str:
    """Return a small, non-sensitive protocol family classification.

    Graph edges created by older extractor versions do not carry the additive
    ``protocol_family`` attribute.  Their edge kind still gives a safe bounded
    fallback for HTTP and pub/sub; RPC edges remain ``unknown`` unless the
    extractor has made an explicit family attribution.
    """

    attrs = edge.get("attributes") if isinstance(edge.get("attributes"), Mapping) else {}
    candidate = str(attrs.get("protocol_family") or "").strip().casefold()
    if candidate in PROTOCOL_FAMILIES:
        return candidate
    edge_kind = str(edge.get("edge_kind") or "").strip().casefold()
    if edge_kind == "http_calls":
        return "http"
    if edge_kind in {"emits", "listens_on"}:
        return "pubsub"
    return "unknown"


def _edge_material(edge: Mapping[str, Any]) -> dict[str, Any]:
    attrs = edge.get("attributes") if isinstance(edge.get("attributes"), Mapping) else {}
    return {
        "edge_kind": _clip(edge.get("edge_kind"), 80),
        "source_node_id": _clip(edge.get("source_node_id"), 160),
        "target_node_id": _clip(edge.get("target_node_id"), 160),
        "confidence": round(max(0.0, min(1.0, float(edge.get("confidence") or 0.0))), 4),
        "unresolved": bool(edge.get("unresolved")),
        "evidence_class": _clip(attrs.get("evidence_class"), 120),
        "protocol_family": _protocol_family(edge),
        "attributes_digest": _digest({str(key): str(value)[:240] for key, value in sorted(attrs.items(), key=lambda item: str(item[0]))}),
    }


def build_service_trace_receipt(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    graph_snapshot_id: str = "",
    graph_digest: str = "",
    max_hops: int = 4,
    max_paths: int = 64,
) -> dict[str, Any]:
    """Build deterministic bounded paths across allowlisted service edges."""

    hops = max(1, min(int(max_hops), MAX_TRACE_HOPS))
    paths_limit = max(1, min(int(max_paths), MAX_TRACE_PATHS))
    node_by_id = {str(node.get("node_id") or ""): node for node in nodes if str(node.get("node_id") or "")}
    adjacency: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    ignored_edges = 0
    accepted_edges: list[Mapping[str, Any]] = []
    for edge in edges:
        kind = str(edge.get("edge_kind") or "")
        source = str(edge.get("source_node_id") or "")
        target = str(edge.get("target_node_id") or "")
        if kind not in ALLOWED_SERVICE_TRACE_EDGE_KINDS or source not in node_by_id or target not in node_by_id or source == target:
            ignored_edges += 1
            continue
        accepted_edges.append(edge)
        adjacency.setdefault(source, []).append((target, edge))
    for source in adjacency:
        adjacency[source] = sorted(adjacency[source], key=lambda item: (str(item[1].get("edge_kind") or ""), item[0]))[:MAX_TRACE_NEIGHBORS]

    boundary_kinds = {"service", "service_endpoint", "service_route", "route", "event_channel", "external_module"}
    starts = sorted({source for source, outgoing in adjacency.items() if outgoing and (str(node_by_id[source].get("node_kind") or "") in boundary_kinds or any(str(edge.get("edge_kind") or "") in {"http_calls", "emits", "listens_on", "depends_on"} for _, edge in outgoing))})
    paths: list[dict[str, Any]] = []
    seen: set[str] = set()
    queue: deque[tuple[str, list[str], list[Mapping[str, Any]]]] = deque((source, [source], []) for source in starts)
    while queue and len(paths) < paths_limit:
        current, node_ids, path_edges = queue.popleft()
        if path_edges:
            edge_material = [_edge_material(edge) for edge in path_edges]
            material = {
                "node_ids": node_ids,
                "edge_kinds": [str(item.get("edge_kind") or "") for item in edge_material],
                "edges": edge_material,
            }
            key = _digest(material)
            if key not in seen:
                seen.add(key)
                paths.append({
                    "path_id": f"trace_path_{key[:24]}",
                    "nodes": [_node_label(node_by_id[node_id]) for node_id in node_ids],
                    "edges": edge_material,
                    "hop_count": len(path_edges),
                    "review_required": True,
                })
        if len(path_edges) >= hops:
            continue
        for target, edge in adjacency.get(current, ()):
            if target in node_ids:
                continue
            queue.append((target, [*node_ids, target], [*path_edges, edge]))

    paths.sort(key=lambda item: str(item.get("path_id") or ""))
    protocol_counts = Counter(_protocol_family(edge) for edge in accepted_edges)
    protocol_attribution = {
        "schema_version": PROTOCOL_ATTRIBUTION_SCHEMA_VERSION,
        "families": sorted(protocol_counts),
        "protocol_counts": {family: protocol_counts[family] for family in sorted(protocol_counts)},
        "unknown_count": protocol_counts.get("unknown", 0),
        "bounded": True,
        "review_required": True,
    }
    material = {
        "schema_version": SERVICE_TRACE_RECEIPT_SCHEMA_VERSION,
        "graph_binding": {"graph_snapshot_id": _clip(graph_snapshot_id, 160), "graph_digest": _clip(graph_digest, 64)},
        "paths": paths[:paths_limit],
        "summary": {
            "node_count": len(node_by_id),
            "allowlisted_edge_count": len(accepted_edges),
            "ignored_edge_count": ignored_edges,
            "path_count": len(paths[:paths_limit]),
            "max_hops": hops,
            "truncated": len(paths) > paths_limit or any(len(value) >= MAX_TRACE_NEIGHBORS for value in adjacency.values()),
            "edge_kinds": sorted({str(edge.get("edge_kind") or "") for edge in accepted_edges}),
            "protocol_counts": protocol_attribution["protocol_counts"],
        },
        "protocol_attribution": protocol_attribution,
    }
    return {
        **material,
        "receipt_digest": _digest(material),
        "authority": "sqlite-authoritative-code-graph",
        "proposal_only": True,
        "promotion": {"status": "not-eligible", "automatic": False, "reason": "trace paths require explicit human review before any edge promotion"},
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "raw_source_returned": False,
            "network": False,
            "compiler_or_lsp": False,
            "trace_edges_promoted": False,
        },
    }


__all__ = [
    "SERVICE_TRACE_RECEIPT_SCHEMA_VERSION",
    "PROTOCOL_ATTRIBUTION_SCHEMA_VERSION",
    "PROTOCOL_FAMILIES",
    "ALLOWED_SERVICE_TRACE_EDGE_KINDS",
    "build_service_trace_receipt",
]
