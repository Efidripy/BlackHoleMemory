"""Read-only cross-repository link proposals for the canonical code graph.

The SQLite graph remains authoritative.  This module deliberately computes a
bounded, non-persistent proposal view over already-published snapshots; it
never writes CROSS_* edges back into the graph or returns source text.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Mapping
from urllib.parse import urlsplit

from .code_graph import SQLiteCodeGraphStore


CROSS_REPO_SCHEMA_VERSION = "bhm.cross-repo-links.v1"
CROSS_EDGE_KINDS = frozenset({
    "CROSS_HTTP_CALLS",
    "CROSS_RPC_CALLS",
    "CROSS_GRPC_CALLS",
    "CROSS_GRAPHQL_CALLS",
    "CROSS_TRPC_CALLS",
    "CROSS_CHANNEL",
    "CROSS_SEMANTICALLY_RELATED",
})
MAX_CROSS_REPO_PROJECTS = 16
MAX_CROSS_REPO_EDGES = 256
MAX_SEMANTIC_DECLARATIONS = 512
_SEMANTIC_EXCLUDED_NAMES = frozenset({"main", "init", "index", "handler", "test", "setup", "run", "start"})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _path_from_endpoint(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"http://{raw}")
    path = parsed.path or (raw if raw.startswith("/") else "")
    normalized = re.sub(r"//+", "/", path or "/")
    return normalized.rstrip("/") or "/"


def _route_path(node: Mapping[str, Any]) -> str:
    attrs = node.get("attributes") or {}
    return str(attrs.get("path") or "").strip()


def _route_matches(endpoint_path: str, route_path: str) -> bool:
    left = _path_from_endpoint(endpoint_path)
    right = _path_from_endpoint(route_path)
    if not left or not right:
        return False
    if left == right:
        return True
    # Permit a route template to match a concrete suffix without broadening
    # to arbitrary substring matches.
    pattern = re.sub(r"\{[^}/]+\}", "[^/]+", right)
    return bool(re.fullmatch(pattern, left))


def _rpc_endpoint(edge: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    attrs = edge.get("attributes") or {}
    if str(attrs.get("protocol") or "").casefold() != "rpc":
        return ""
    return str(attrs.get("endpoint") or target.get("name") or target.get("qualified_name") or "").strip()


def _semantic_declaration_key(node: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Return an exact metadata signature key without retaining source text."""

    kind = str(node.get("node_kind") or "").strip().casefold()
    name = str(node.get("name") or "").strip().casefold()
    if kind not in {"function", "method", "class", "interface", "enum", "struct", "record", "trait", "object", "type", "message", "service", "namespace"}:
        return None
    if len(name) < 4 or name in _SEMANTIC_EXCLUDED_NAMES:
        return None
    signature = str(node.get("signature") or node.get("qualified_name") or "").strip().casefold()
    if not signature:
        return None
    return kind, name, _sha256(f"{kind}|{name}|{signature}")[:24]


def _current_graph_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT current.project, current.root_id, current.graph_snapshot_id,
               snapshots.root_path, snapshots.graph_digest
        FROM repository_code_graph_current AS current
        JOIN repository_code_graph_snapshots AS snapshots
          ON snapshots.graph_snapshot_id = current.graph_snapshot_id
        WHERE snapshots.status = 'completed'
        ORDER BY current.project, current.root_id
        LIMIT ?
        """,
        (MAX_CROSS_REPO_PROJECTS,),
    ).fetchall()


def build_cross_repo_link_preview(
    database_path: str,
    *,
    limit: int = 64,
    project: str | None = None,
) -> dict[str, Any]:
    """Build bounded CROSS_* proposals from published graph snapshots."""

    bounded_limit = max(1, min(int(limit), MAX_CROSS_REPO_EDGES))
    store = SQLiteCodeGraphStore(database_path)
    schema = store.inspect_schema()
    if not schema.get("ready"):
        return {
            "schema_version": CROSS_REPO_SCHEMA_VERSION,
            "cross_edges": [],
            "counts": {},
            "projects": [],
            "execution": {"writes_sqlite_state": False, "raw_source_returned": False, "cross_edges_promoted": False},
            "provenance": {"authority": "proposal", "source": "published-sqlite-graph"},
        }
    connection = store._connect(read_only=True)
    try:
        rows = _current_graph_rows(connection)
    finally:
        connection.close()
    if project and not project_scope_is_aggregate(project):
        rows = [row for row in rows if str(row["project"]) == str(project)]
    materials: list[dict[str, Any]] = []
    for row in rows:
        material = store.snapshot(str(row["graph_snapshot_id"]), include_material=True, read_only=True)
        material["project"] = str(row["project"])
        material["root_id"] = str(row["root_id"])
        materials.append(material)
    route_index: list[tuple[dict[str, Any], dict[str, Any]]] = []
    channel_index: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rpc_index: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    semantic_index: dict[tuple[str, str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for material in materials:
        nodes = {str(node.get("node_id")): node for node in material.get("nodes") or []}
        for node in nodes.values():
            if node.get("node_kind") == "route" and _route_path(node):
                route_index.append((material, node))
            if node.get("node_kind") == "event_channel":
                channel_index.append((material, node))
            semantic_key = _semantic_declaration_key(node)
            if semantic_key:
                semantic_index.setdefault(semantic_key, []).append((material, node))
        for edge in material.get("edges") or []:
            target = nodes.get(str(edge.get("target_node_id"))) or {}
            endpoint = _rpc_endpoint(edge, target)
            if endpoint and target.get("node_kind") == "service_endpoint":
                rpc_index.append((material, target, endpoint))
    semantic_index = {
        key: values[:MAX_SEMANTIC_DECLARATIONS]
        for key, values in semantic_index.items()
    }
    proposals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for material in materials:
        nodes = {str(node.get("node_id")): node for node in material.get("nodes") or []}
        for edge in material.get("edges") or []:
            source = nodes.get(str(edge.get("source_node_id"))) or {}
            target = nodes.get(str(edge.get("target_node_id"))) or {}
            kind = str(edge.get("edge_kind") or "")
            source_project = str(material.get("project") or "")
            if kind == "http_calls" and target.get("node_kind") in {"service_endpoint", "service_route"}:
                endpoint = str((edge.get("attributes") or {}).get("endpoint") or target.get("name") or "")
                for remote, route in route_index:
                    if remote is material or str(remote.get("project")) == source_project:
                        continue
                    if not _route_matches(endpoint, _route_path(route)):
                        continue
                    proposal = {
                        "edge_kind": "CROSS_HTTP_CALLS",
                        "source_project": source_project,
                        "target_project": str(remote.get("project") or ""),
                        "source_root_id": material.get("root_id"),
                        "target_root_id": remote.get("root_id"),
                        "source_node_id": source.get("node_id"),
                        "target_node_id": route.get("node_id"),
                        "source_graph_snapshot_id": material.get("graph_snapshot_id"),
                        "target_graph_snapshot_id": remote.get("graph_snapshot_id"),
                        "confidence": 0.76,
                        "evidence": {"endpoint_path": _path_from_endpoint(endpoint), "route_path": _route_path(route), "source_edge_kind": kind, "review_required": True},
                    }
                    key = _sha256(proposal)
                    if key not in seen:
                        seen.add(key)
                        proposal["proposal_id"] = f"cross_{key[:24]}"
                        proposals.append(proposal)
            if kind == "depends_on" and target.get("node_kind") == "service_endpoint":
                endpoint = _rpc_endpoint(edge, target)
                if endpoint:
                    endpoint_digest = _sha256(endpoint)[:16]
                    for remote, remote_endpoint, remote_value in rpc_index:
                        if remote is material or str(remote.get("project")) == source_project:
                            continue
                        if remote_value != endpoint:
                            continue
                        proposal = {
                            "edge_kind": "CROSS_RPC_CALLS",
                            "source_project": source_project,
                            "target_project": str(remote.get("project") or ""),
                            "source_root_id": material.get("root_id"),
                            "target_root_id": remote.get("root_id"),
                            "source_node_id": source.get("node_id"),
                            "target_node_id": remote_endpoint.get("node_id"),
                            "source_graph_snapshot_id": material.get("graph_snapshot_id"),
                            "target_graph_snapshot_id": remote.get("graph_snapshot_id"),
                            "confidence": 0.62,
                            "evidence": {
                                "endpoint_digest": endpoint_digest,
                                "protocol": "rpc",
                                "source_edge_kind": kind,
                                "evidence_class": "literal-rpc-both-snapshots",
                                "review_required": True,
                            },
                        }
                        key = _sha256(proposal)
                        if key not in seen:
                            seen.add(key)
                            proposal["proposal_id"] = f"cross_{key[:24]}"
                            proposals.append(proposal)
            if kind in {"emits", "listens_on"} and target.get("node_kind") == "event_channel":
                channel = str((edge.get("attributes") or {}).get("channel") or target.get("name") or "")
                for remote, remote_channel in channel_index:
                    if remote is material or str(remote.get("project")) == source_project or str(remote_channel.get("name")) != channel:
                        continue
                    proposal = {
                        "edge_kind": "CROSS_CHANNEL",
                        "source_project": source_project,
                        "target_project": str(remote.get("project") or ""),
                        "source_root_id": material.get("root_id"),
                        "target_root_id": remote.get("root_id"),
                        "source_node_id": source.get("node_id"),
                        "target_node_id": remote_channel.get("node_id"),
                        "source_graph_snapshot_id": material.get("graph_snapshot_id"),
                        "target_graph_snapshot_id": remote.get("graph_snapshot_id"),
                        "confidence": 0.7,
                        "evidence": {"channel": channel, "source_edge_kind": kind, "review_required": True},
                    }
                    key = _sha256(proposal)
                    if key not in seen:
                        seen.add(key)
                        proposal["proposal_id"] = f"cross_{key[:24]}"
                        proposals.append(proposal)
            if len(proposals) >= bounded_limit:
                break
        if len(proposals) >= bounded_limit:
            break
    if len(proposals) < bounded_limit:
        # Exact declaration metadata matches are a proposal-only semantic
        # channel.  We emit one deterministic pair per cross-project match;
        # no source text or embedding payload is persisted or returned.
        for semantic_key, matches in sorted(semantic_index.items()):
            kind, name, signature_digest = semantic_key
            if len(matches) < 2:
                continue
            for index, (source_material, source_node) in enumerate(matches):
                for target_material, target_node in matches[index + 1 :]:
                    source_project = str(source_material.get("project") or "")
                    target_project = str(target_material.get("project") or "")
                    if not source_project or source_project == target_project:
                        continue
                    # Similarity is symmetric; choose lexical project/node
                    # order so a pair is emitted once and reproducibly.
                    left = (source_project, str(source_node.get("node_id") or ""))
                    right = (target_project, str(target_node.get("node_id") or ""))
                    if left > right:
                        source_material, target_material = target_material, source_material
                        source_node, target_node = target_node, source_node
                        source_project, target_project = target_project, source_project
                    proposal = {
                        "edge_kind": "CROSS_SEMANTICALLY_RELATED",
                        "source_project": source_project,
                        "target_project": target_project,
                        "source_root_id": source_material.get("root_id"),
                        "target_root_id": target_material.get("root_id"),
                        "source_node_id": source_node.get("node_id"),
                        "target_node_id": target_node.get("node_id"),
                        "source_graph_snapshot_id": source_material.get("graph_snapshot_id"),
                        "target_graph_snapshot_id": target_material.get("graph_snapshot_id"),
                        "confidence": 0.55,
                        "evidence": {
                            "node_kind": kind,
                            "name_digest": _sha256(name)[:16],
                            "signature_digest": signature_digest,
                            "basis": "exact-declaration-metadata",
                            "review_required": True,
                        },
                    }
                    key = _sha256(proposal)
                    if key not in seen:
                        seen.add(key)
                        proposal["proposal_id"] = f"cross_{key[:24]}"
                        proposals.append(proposal)
                    if len(proposals) >= bounded_limit:
                        break
                if len(proposals) >= bounded_limit:
                    break
            if len(proposals) >= bounded_limit:
                break
    proposals = proposals[:bounded_limit]
    counts: dict[str, int] = {}
    for proposal in proposals:
        kind = str(proposal["edge_kind"])
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "schema_version": CROSS_REPO_SCHEMA_VERSION,
        "cross_edges": proposals,
        "counts": counts,
        "projects": [{"project": item.get("project"), "root_id": item.get("root_id"), "graph_snapshot_id": item.get("graph_snapshot_id"), "graph_digest": item.get("graph_digest")} for item in materials],
        "execution": {"writes_sqlite_state": False, "raw_source_returned": False, "cross_edges_promoted": False},
        "provenance": {"authority": "proposal", "source": "published-sqlite-graph", "review_required": True},
    }


def project_scope_is_aggregate(project: str | None) -> bool:
    """Return whether a cross-repository request asks for aggregate scope."""

    return str(project or "").strip().casefold() in {"", ".", "*", "blackholememory"}


__all__ = [
    "CROSS_EDGE_KINDS",
    "CROSS_REPO_SCHEMA_VERSION",
    "build_cross_repo_link_preview",
    "project_scope_is_aggregate",
]
