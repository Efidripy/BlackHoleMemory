"""Bounded architecture projections derived from the authoritative code graph.

The helpers intentionally operate on graph metadata only.  They never return
source text, never mutate SQLite/Qdrant and only emit reviewable proposals.
"""

from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import json
from typing import Any, Mapping, Sequence


ARCHITECTURE_INTELLIGENCE_SCHEMA_VERSION = "bhm.architecture-intelligence.v1"
ARCHITECTURE_MEMORY_SCHEMA_VERSION = "bhm.architecture-memory.v1"
ARCHITECTURE_EXPLAIN_RECEIPT_SCHEMA_VERSION = "bhm.architecture-explain-receipt.v1"
GRAPH_ANALYSIS_QUALITY_SCHEMA_VERSION = "bhm.graph-analysis-quality.v1"
_DEPENDENCY_EDGE_KINDS = {"imports", "depends_on", "exposes", "route_handles", "http_calls", "emits", "listens_on"}
_CALL_EDGE_KINDS = {"calls", "async_calls", "route_handles", "tests"}
_CLONE_NODE_KINDS = {"function", "method", "class", "interface", "type", "enum", "struct", "module", "package"}
_ENTRYPOINT_NAMES = {"main", "app", "cli", "run", "start", "handler", "lambda_handler"}


def build_architecture_intelligence(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    max_items: int = 32,
) -> dict[str, Any]:
    """Return bounded clusters, dead-code candidates and clone hints."""

    limit = max(1, min(int(max_items), 64))
    node_by_id = {str(node.get("node_id") or ""): node for node in nodes if str(node.get("node_id") or "")}
    outgoing: dict[str, set[str]] = defaultdict(set)
    incoming: dict[str, set[str]] = defaultdict(set)
    import_adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        source = str(edge.get("source_node_id") or "")
        target = str(edge.get("target_node_id") or "")
        kind = str(edge.get("edge_kind") or "")
        if source not in node_by_id or target not in node_by_id:
            continue
        if kind in _CALL_EDGE_KINDS:
            outgoing[source].add(target)
            incoming[target].add(source)
        if kind in _DEPENDENCY_EDGE_KINDS:
            import_adjacency[source].add(target)
            import_adjacency[target].add(source)

    clusters = _clusters(node_by_id, import_adjacency, limit)
    communities = _bounded_communities(node_by_id, import_adjacency, limit)
    dead_code = _dead_code_candidates(node_by_id, incoming, outgoing, limit)
    clone_hints = _clone_hints(node_by_id, limit)
    return {
        "schema_version": ARCHITECTURE_INTELLIGENCE_SCHEMA_VERSION,
        "clusters": clusters,
        "communities": communities,
        "dead_code_candidates": dead_code,
        "clone_hints": clone_hints,
        "limits": {"max_items": limit},
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "raw_source_returned": False,
            "auto_apply": False,
            "authority": "proposal",
        },
    }


def build_architecture_explain_receipt(
    intelligence: Mapping[str, Any],
    *,
    graph_snapshot_id: str,
    graph_digest: str,
    max_items: int = 32,
) -> dict[str, Any]:
    """Explain bounded architecture signals with a deterministic receipt.

    The receipt is intentionally a metadata-only audit projection.  It
    records which bounded algorithms produced each signal and the exact
    graph binding, without returning source text or authorizing a change.
    Consumers can compare ``receipt_digest`` before displaying or caching a
    proposal; a changed graph necessarily produces a different receipt.
    """

    limit = max(1, min(int(max_items), 64))
    clusters = list(intelligence.get("clusters") or [])[:limit]
    communities = list(intelligence.get("communities") or [])[:limit]
    dead_code = list(intelligence.get("dead_code_candidates") or [])[:limit]
    clone_hints = list(intelligence.get("clone_hints") or [])[:limit]
    evidence = {
        "graph_snapshot_id": str(graph_snapshot_id or ""),
        "graph_digest": str(graph_digest or ""),
        "limits": {"max_items": limit, "community_nodes": 256, "community_iterations": 4},
        "clusters": [
            {"cluster_id": str(item.get("cluster_id") or ""), "node_count": int(item.get("node_count") or 0)}
            for item in clusters
        ],
        "communities": [
            {
                "community_id": str(item.get("community_id") or ""),
                "node_count": int(item.get("node_count") or 0),
                "algorithm": str(item.get("algorithm") or ""),
                "iterations": int(item.get("iterations") or 0),
                "approximation": bool(item.get("approximation")),
            }
            for item in communities
        ],
        "dead_code": [
            {
                "node_id": str(item.get("node_id") or ""),
                "node_kind": str(item.get("node_kind") or ""),
                "incoming_call_count": int(item.get("incoming_call_count") or 0),
                "outgoing_call_count": int(item.get("outgoing_call_count") or 0),
                "reason": str(item.get("reason") or ""),
            }
            for item in dead_code
        ],
        "clone_hints": [
            {
                "clone_key": str(item.get("clone_key") or ""),
                "similarity": str(item.get("similarity") or ""),
                "member_count": int(item.get("member_count") or 0),
            }
            for item in clone_hints
        ],
    }
    canonical = json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    receipt_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "schema_version": ARCHITECTURE_EXPLAIN_RECEIPT_SCHEMA_VERSION,
        "receipt_id": f"architecture-explain-{receipt_digest[:16]}",
        "receipt_digest": receipt_digest,
        "binding": {
            "graph_snapshot_id": str(graph_snapshot_id or ""),
            "graph_digest": str(graph_digest or ""),
            "authority": "sqlite-authoritative-graph",
        },
        "evidence": evidence,
        "explanations": {
            "clusters": "connected components over allowlisted dependency metadata",
            "communities": "bounded-label-propagation-v1; approximate, four iterations maximum",
            "dead_code": "candidate has no inbound bounded caller/test/route edge; not a proof of dead code",
            "clone_hints": "exact content digest or bounded signature match; not semantic clone proof",
        },
        "limitations": [
            "metadata_only",
            "bounded_graph_snapshot",
            "human_review_required",
            "no_runtime_topology_proof",
            "no_source_text",
        ],
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "writes_mem0": False,
            "raw_source_returned": False,
            "auto_apply": False,
            "authority": "proposal",
            "human_review_required": True,
        },
    }


def build_architecture_memory(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    graph_snapshot_id: str,
    graph_digest: str,
    repository_snapshot_id: str | None = None,
    max_items: int = 32,
) -> dict[str, Any]:
    """Bind bounded architecture proposals to one immutable graph snapshot.

    This is a metadata-only architecture-memory projection.  It deliberately
    keeps proposals separate from convention authority and never persists or
    promotes inferred structure.  Consumers can safely cache the projection
    by ``graph_digest`` and reject it when the graph changes.
    """

    intelligence = build_architecture_intelligence(nodes, edges, max_items=max_items)
    explain_receipt = build_architecture_explain_receipt(
        intelligence,
        graph_snapshot_id=graph_snapshot_id,
        graph_digest=graph_digest,
        max_items=max_items,
    )
    quality_receipt = build_graph_analysis_quality_receipt(
        intelligence,
        graph_snapshot_id=graph_snapshot_id,
        graph_digest=graph_digest,
        node_count=len(nodes),
        edge_count=len(edges),
        max_items=max_items,
    )
    return {
        "schema_version": ARCHITECTURE_MEMORY_SCHEMA_VERSION,
        "binding": {
            "graph_snapshot_id": str(graph_snapshot_id or ""),
            "graph_digest": str(graph_digest or ""),
            "repository_snapshot_id": str(repository_snapshot_id or ""),
            "authority": "sqlite-authoritative-graph",
        },
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "cluster_count": len(intelligence["clusters"]),
            "community_count": len(intelligence["communities"]),
            "dead_code_candidate_count": len(intelligence["dead_code_candidates"]),
            "clone_hint_count": len(intelligence["clone_hints"]),
        },
        "proposals": {
            "clusters": intelligence["clusters"],
            "communities": intelligence["communities"],
            "dead_code_candidates": intelligence["dead_code_candidates"],
            "clone_hints": intelligence["clone_hints"],
            "explain_receipt": explain_receipt,
            "quality_receipt": quality_receipt,
        },
        "gaps": [
            "runtime_topology_not_inferred",
            "convention_acceptance_requires_human_review",
            "proposal_not_authoritative",
        ],
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "writes_mem0": False,
            "raw_source_returned": False,
            "auto_apply": False,
            "model_started": False,
            "network_access": False,
            "authority": "proposal",
            "human_review_required": True,
        },
    }


def build_graph_analysis_quality_receipt(
    intelligence: Mapping[str, Any],
    *,
    graph_snapshot_id: str,
    graph_digest: str,
    node_count: int,
    edge_count: int,
    max_items: int = 32,
) -> dict[str, Any]:
    """Return a deterministic quality/coverage receipt for graph proposals."""

    limit = max(1, min(int(max_items), 64))
    clusters = list(intelligence.get("clusters") or [])[:limit]
    communities = list(intelligence.get("communities") or [])[:limit]
    dead_code = list(intelligence.get("dead_code_candidates") or [])[:limit]
    clone_hints = list(intelligence.get("clone_hints") or [])[:limit]
    community_nodes = sorted({str(node_id) for item in communities for node_id in (item.get("node_ids") or []) if str(node_id)})[:256]
    algorithms = sorted({str(item.get("algorithm") or "unknown") for item in communities})
    approximation = any(bool(item.get("approximation")) for item in communities)
    truncation = any(len(list(intelligence.get(key) or [])) > limit for key in ("clusters", "communities", "dead_code_candidates", "clone_hints"))
    evidence = {
        "graph_snapshot_id": str(graph_snapshot_id or "")[:128],
        "graph_digest": str(graph_digest or "")[:128],
        "node_count": max(0, int(node_count)),
        "edge_count": max(0, int(edge_count)),
        "limits": {"max_items": limit, "community_nodes": 256, "community_iterations": 4},
        "counts": {"clusters": len(clusters), "communities": len(communities), "community_nodes": len(community_nodes), "dead_code_candidates": len(dead_code), "clone_hints": len(clone_hints)},
        "algorithms": algorithms,
        "approximation": approximation,
        "truncated": truncation,
    }
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "schema_version": GRAPH_ANALYSIS_QUALITY_SCHEMA_VERSION,
        "status": "partial" if approximation or truncation else "complete",
        "receipt_digest": digest,
        "binding": {"graph_snapshot_id": str(graph_snapshot_id or "")[:128], "graph_digest": str(graph_digest or "")[:128], "authority": "sqlite-authoritative-graph"},
        "evidence": evidence,
        "coverage": {"community_node_ratio": round(len(community_nodes) / max(min(int(node_count), 256), 1), 6), "community_node_cap_applied": min(int(node_count), 256) < int(node_count)},
        "limitations": ["metadata_only", "bounded_graph_snapshot", "approximate_community_algorithm", "no_semantic_clone_proof", "human_review_required"],
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_mem0": False, "raw_source_returned": False, "auto_apply": False, "authority": "proposal", "human_review_required": True},
    }


def _bounded_communities(
    node_by_id: Mapping[str, Mapping[str, Any]],
    adjacency: Mapping[str, set[str]],
    limit: int,
) -> list[dict[str, Any]]:
    """Return deterministic, bounded community proposals.

    This is a small label-propagation approximation, not a Louvain claim. It
    operates on graph metadata only, caps the working set and exposes its
    algorithm/iteration bounds so callers cannot mistake it for a whole
    program proof or an authoritative partition.
    """

    members = sorted(node_by_id)[:256]
    labels = {node_id: node_id for node_id in members}
    adjacency_limited = {
        node_id: {neighbor for neighbor in adjacency.get(node_id, set()) if neighbor in labels}
        for node_id in members
    }
    iterations = 0
    for iteration in range(1, 5):
        changed = False
        for node_id in members:
            neighbors = adjacency_limited[node_id]
            if not neighbors:
                continue
            counts: dict[str, int] = defaultdict(int)
            for neighbor in neighbors:
                counts[labels[neighbor]] += 1
            best = min(
                (label for label, count in counts.items() if count == max(counts.values())),
                default=labels[node_id],
            )
            if best != labels[node_id]:
                labels[node_id] = best
                changed = True
        iterations = iteration
        if not changed:
            break
    groups: dict[str, list[str]] = defaultdict(list)
    for node_id, label in labels.items():
        groups[label].append(node_id)
    result: list[dict[str, Any]] = []
    for label, group in groups.items():
        if len(group) < 2:
            continue
        paths = sorted({str(node_by_id[item].get("path") or "") for item in group})
        result.append(
            {
                "community_id": f"community-{len(result) + 1:03d}",
                "node_count": len(group),
                "node_ids": sorted(group)[:256],
                "paths": paths[:64],
                "algorithm": "bounded-label-propagation-v1",
                "iterations": iterations,
                "approximation": True,
                "review_required": True,
            }
        )
    return sorted(result, key=lambda item: (-int(item["node_count"]), str(item["community_id"])))[:limit]


def _clusters(node_by_id: Mapping[str, Mapping[str, Any]], adjacency: Mapping[str, set[str]], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for node_id in sorted(node_by_id):
        if node_id in seen or not adjacency.get(node_id):
            continue
        queue = deque([node_id])
        members: list[str] = []
        seen.add(node_id)
        while queue and len(members) < 256:
            current = queue.popleft()
            members.append(current)
            for neighbor in sorted(adjacency.get(current, set())):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        paths = sorted({str(node_by_id[item].get("path") or "") for item in members if node_by_id.get(item)})
        result.append({"cluster_id": f"cluster-{len(result) + 1:03d}", "node_count": len(members), "node_ids": sorted(members)[:256], "paths": paths[:64]})
    result.sort(key=lambda item: (-int(item["node_count"]), str(item["cluster_id"])))
    return result[:limit]


def _dead_code_candidates(
    node_by_id: Mapping[str, Mapping[str, Any]],
    incoming: Mapping[str, set[str]],
    outgoing: Mapping[str, set[str]],
    limit: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for node_id, node in node_by_id.items():
        kind = str(node.get("node_kind") or "")
        name = str(node.get("name") or "")
        path = str(node.get("path") or "")
        if kind not in _CLONE_NODE_KINDS or not name or name.startswith("_") or name.casefold() in _ENTRYPOINT_NAMES:
            continue
        if "/test" in path.casefold() or path.casefold().startswith("test"):
            continue
        inbound = len(incoming.get(node_id, set()))
        outbound = len(outgoing.get(node_id, set()))
        if inbound:
            continue
        candidates.append(
            {
                "node_id": node_id,
                "path": path,
                "name": name,
                "node_kind": kind,
                "incoming_call_count": inbound,
                "outgoing_call_count": outbound,
                "reason": "no bounded callers/tests/routes in current graph snapshot",
                "source_ref": (node.get("provenance") or {}).get("source_ref") or "",
            }
        )
    return sorted(candidates, key=lambda item: (str(item["path"]), str(item["name"])))[:limit]


def _clone_hints(node_by_id: Mapping[str, Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for node in node_by_id.values():
        if str(node.get("node_kind") or "") not in _CLONE_NODE_KINDS:
            continue
        content_hash = str(node.get("content_sha256") or "")
        signature = str(node.get("signature") or "").strip().casefold()
        key = f"hash:{content_hash}" if content_hash else f"signature:{signature[:160]}"
        if key.endswith(":") or key.endswith("signature:"):
            continue
        groups[key].append(node)
    hints: list[dict[str, Any]] = []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        members = sorted(
            ({"node_id": str(node.get("node_id") or ""), "path": str(node.get("path") or ""), "name": str(node.get("name") or "")} for node in group),
            key=lambda item: (item["path"], item["name"], item["node_id"]),
        )
        hints.append({"clone_key": key[:180], "similarity": "exact-metadata-match", "member_count": len(members), "members": members[:32], "review_required": True})
    return sorted(hints, key=lambda item: (-int(item["member_count"]), str(item["clone_key"])))[:limit]
