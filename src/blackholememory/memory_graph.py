"""SQLite-authoritative temporal memory graph for WI-06.

Graph rows are rebuildable projections inside the same authoritative SQLite
database. The module never stores raw memory/transcript content; every node and
edge carries bounded payload metadata and source provenance.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from .resource_limits import SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS


MEMORY_GRAPH_SCHEMA_VERSION = "bhm.memory-graph.v1"
MEMORY_GRAPH_BUILD_VERSION = "wi06-native-1"
MEMORY_GRAPH_MAX_NODES = 2_048
MEMORY_GRAPH_MAX_EDGES = 4_096
MEMORY_GRAPH_MAX_QUARANTINE = 512
MEMORY_GRAPH_MAX_TEXT = 480
MEMORY_GRAPH_OPERATIONS = ("as_of", "search", "neighborhood", "supersession")


class MemoryGraphError(ValueError):
    """Raised when a graph build/query violates deterministic bounds."""


def build_memory_graph(
    database_path: Path | str,
    *,
    project: str,
    records: Sequence[Mapping[str, Any]] = (),
    links: Sequence[Mapping[str, Any]] = (),
    observations: Sequence[Mapping[str, Any]] = (),
    session_records: Sequence[Mapping[str, Any]] = (),
    tasks: Sequence[Mapping[str, Any]] = (),
    adrs: Sequence[Mapping[str, Any]] = (),
    documents: Sequence[Mapping[str, Any]] = (),
    as_of: str | None = None,
    fail_after_stage: str | None = None,
) -> dict[str, Any]:
    """Build and publish a deterministic graph snapshot transactionally."""

    project_name = _required_project(project)
    effective_as_of = _normalize_time(as_of) if as_of else None
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    nodes, quarantined = _collect_nodes(
        project_name,
        (
            ("memory", records),
            ("observation", observations),
            ("session", session_records),
            ("task", tasks),
            ("adr", adrs),
            ("document", documents),
        ),
    )
    edges, edge_quarantine = _collect_edges(project_name, links, nodes)
    quarantined.extend(edge_quarantine)
    if len(nodes) > MEMORY_GRAPH_MAX_NODES:
        raise MemoryGraphError(f"node count exceeds {MEMORY_GRAPH_MAX_NODES}")
    if len(edges) > MEMORY_GRAPH_MAX_EDGES:
        raise MemoryGraphError(f"edge count exceeds {MEMORY_GRAPH_MAX_EDGES}")
    quarantined = quarantined[:MEMORY_GRAPH_MAX_QUARANTINE]
    graph_digest = _sha256(_canonical_json({"nodes": nodes, "edges": edges, "quarantine": quarantined, "as_of": effective_as_of}))
    snapshot_id = f"mgraph_{_sha256(f'{project_name}:{graph_digest}:{effective_as_of or ""}')[:24]}"
    now = _utc_now()
    summary = {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "quarantine_count": len(quarantined),
        "node_types": _counts(nodes, "entity_type"),
        "edge_types": _counts(edges, "relation"),
        "invalid_temporal_count": sum(1 for item in quarantined if item.get("reason") == "invalid_temporal_interval"),
        "unresolved_edge_count": sum(1 for item in quarantined if item.get("reason") == "unresolved_endpoint"),
    }
    connection = _connect_rw(path)
    try:
        _initialize_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM memory_graph_nodes WHERE snapshot_id = ?", (snapshot_id,))
        connection.execute("DELETE FROM memory_graph_edges WHERE snapshot_id = ?", (snapshot_id,))
        connection.execute("DELETE FROM memory_graph_quarantine WHERE snapshot_id = ?", (snapshot_id,))
        connection.execute("DELETE FROM memory_graph_snapshots WHERE snapshot_id = ?", (snapshot_id,))
        connection.execute(
            "INSERT INTO memory_graph_snapshots(snapshot_id, project, graph_digest, build_version, status, as_of, summary_json, created_at) VALUES (?, ?, ?, ?, 'building', ?, ?, ?)",
            (snapshot_id, project_name, graph_digest, MEMORY_GRAPH_BUILD_VERSION, effective_as_of, _canonical_json(summary), now),
        )
        for node in nodes:
            connection.execute(
                "INSERT INTO memory_graph_nodes(snapshot_id,node_key,project,entity_type,entity_id,valid_from,valid_until,recorded_at,source_kind,source_id,source_sha256,payload_json,node_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id, node["node_key"], project_name, node["entity_type"], node["entity_id"], node["valid_from"], node["valid_until"], node["recorded_at"], node["source_kind"], node["source_id"], node["source_sha256"], _canonical_json(node["payload"]), node["node_sha256"]),
            )
        for edge in edges:
            connection.execute(
                "INSERT INTO memory_graph_edges(snapshot_id,edge_key,project,source_node_key,target_node_key,relation,valid_from,valid_until,recorded_at,source_kind,source_id,source_sha256,confidence,payload_json,edge_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id, edge["edge_key"], project_name, edge["source_node_key"], edge["target_node_key"], edge["relation"], edge["valid_from"], edge["valid_until"], edge["recorded_at"], edge["source_kind"], edge["source_id"], edge["source_sha256"], edge["confidence"], _canonical_json(edge["payload"]), edge["edge_sha256"]),
            )
        for item in quarantined:
            connection.execute(
                "INSERT INTO memory_graph_quarantine(snapshot_id,project,entity_type,entity_id,reason,payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (snapshot_id, project_name, item.get("entity_type", ""), item.get("entity_id", ""), item.get("reason", "unknown"), _canonical_json(item.get("payload") or {}), now),
            )
        if fail_after_stage == "before_publish":
            raise MemoryGraphError("injected publish failure")
        connection.execute("UPDATE memory_graph_snapshots SET status = 'published' WHERE snapshot_id = ?", (snapshot_id,))
        connection.execute(
            "INSERT INTO memory_graph_current(project, snapshot_id, updated_at) VALUES (?, ?, ?) ON CONFLICT(project) DO UPDATE SET snapshot_id = excluded.snapshot_id, updated_at = excluded.updated_at",
            (project_name, snapshot_id, now),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "schema_version": MEMORY_GRAPH_SCHEMA_VERSION,
        "ok": True,
        "action": "build",
        "project": project_name,
        "snapshot_id": snapshot_id,
        "graph_digest": graph_digest,
        "summary": summary,
        "quarantine": quarantined,
        "execution": {"writes_sqlite": True, "writes_qdrant": False, "writes_mem0": False, "model_started": False, "public_mcp": False},
    }


def query_memory_graph(
    database_path: Path | str,
    *,
    project: str,
    operation: str = "as_of",
    query: str = "",
    snapshot_id: str | None = None,
    as_of: str | None = None,
    depth: int = 2,
    limit: int = 32,
    max_tokens: int = 4_096,
    time_budget_ms: float = 500.0,
) -> dict[str, Any]:
    """Read a bounded graph snapshot without creating SQLite state."""

    project_name = _required_project(project)
    if operation not in MEMORY_GRAPH_OPERATIONS:
        raise MemoryGraphError(f"unsupported operation: {operation}")
    if not 0 <= int(depth) <= 8:
        raise MemoryGraphError("depth must be between 0 and 8")
    if not 1 <= int(limit) <= 128:
        raise MemoryGraphError("limit must be between 1 and 128")
    if not 128 <= int(max_tokens) <= 16_384:
        raise MemoryGraphError("max_tokens must be between 128 and 16384")
    if float(time_budget_ms) <= 0 or float(time_budget_ms) > 5_000:
        raise MemoryGraphError("time_budget_ms must be between 0 and 5000")
    selected_as_of = _normalize_time(as_of) if as_of else None
    started = time.perf_counter()
    path = Path(database_path)
    if not path.exists():
        raise MemoryGraphError("memory graph database unavailable")
    connection = _connect_ro(path)
    try:
        current = connection.execute("SELECT snapshot_id FROM memory_graph_current WHERE project = ?", (project_name,)).fetchone()
        selected = str(snapshot_id or (current[0] if current else ""))
        if not selected:
            raise MemoryGraphError("memory graph snapshot unavailable")
        snapshot = connection.execute("SELECT * FROM memory_graph_snapshots WHERE snapshot_id = ? AND project = ?", (selected, project_name)).fetchone()
        if snapshot is None or str(snapshot["status"]) != "published":
            raise MemoryGraphError("memory graph snapshot unavailable")
        nodes = _read_nodes(connection, selected, project_name, selected_as_of)
        edges = _read_edges(connection, selected, project_name, selected_as_of)
        if operation == "search":
            needle = str(query or "").casefold()
            nodes = [item for item in nodes if needle in str(item.get("entity_id", "")).casefold() or needle in str(item.get("payload", {})).casefold()][: int(limit)]
            node_keys = {item["node_key"] for item in nodes}
            edges = [item for item in edges if item["source_node_key"] in node_keys or item["target_node_key"] in node_keys][: int(limit)]
        elif operation == "supersession":
            edges = [item for item in edges if item["relation"] == "supersedes" and (not query or query in item["source_node_key"] or query in item["target_node_key"])][: int(limit)]
            keys = {item["source_node_key"] for item in edges} | {item["target_node_key"] for item in edges}
            nodes = [item for item in nodes if item["node_key"] in keys][: int(limit)]
        elif operation == "neighborhood":
            nodes, edges = _neighborhood(nodes, edges, query=query, depth=int(depth), limit=int(limit))
        else:
            nodes = nodes[: int(limit)]
            edges = edges[: int(limit)]
        payload = {
            "project": project_name,
            "snapshot_id": selected,
            "graph_digest": str(snapshot["graph_digest"]),
            "operation": operation,
            "as_of": selected_as_of,
            "nodes": nodes,
            "edges": edges,
        }
        estimated_tokens = _estimate_tokens(payload)
        truncated = estimated_tokens > max_tokens
        omissions: list[str] = []
        while estimated_tokens > max_tokens and (payload["edges"] or payload["nodes"]):
            if payload["edges"]:
                payload["edges"].pop()
            else:
                payload["nodes"].pop()
            omissions.append("items_truncated_to_token_budget")
            estimated_tokens = _estimate_tokens(payload)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        response_core = {**payload, "estimated_tokens": estimated_tokens, "truncated": truncated, "omissions": sorted(set(omissions))}
        return {
            "schema_version": MEMORY_GRAPH_SCHEMA_VERSION,
            "ok": True,
            "response_digest": _sha256(_canonical_json(response_core)),
            **response_core,
            "provenance": {
                "complete": all(item.get("provenance") for item in payload["nodes"] + payload["edges"]),
                "authority": "sqlite-authoritative",
                "snapshot_id": selected,
                "graph_digest": str(snapshot["graph_digest"]),
            },
            "budget": {"max_tokens": int(max_tokens), "estimated_tokens": estimated_tokens, "time_budget_ms": float(time_budget_ms), "elapsed_ms": round(elapsed_ms, 3), "within_time_budget": elapsed_ms <= float(time_budget_ms)},
            "execution": {"writes_sqlite": False, "writes_qdrant": False, "writes_mem0": False, "model_started": False, "raw_source_returned": False, "public_mcp": False},
        }
    finally:
        connection.close()


def explain_memory_graph(database_path: Path | str, **kwargs: Any) -> dict[str, Any]:
    result = query_memory_graph(database_path, **kwargs)
    result["explain"] = {
        "operation": result.get("operation"),
        "as_of": result.get("as_of"),
        "reason_codes": ["temporal_filter_applied" if result.get("as_of") else "current_lkg_snapshot", "provenance_attached", "bounded_read_only"],
    }
    return result


def _collect_nodes(project: str, sources: Sequence[tuple[str, Sequence[Mapping[str, Any]]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    quarantine: list[dict[str, Any]] = []
    for entity_type, records in sources:
        for raw in list(records)[:MEMORY_GRAPH_MAX_NODES]:
            item = dict(raw)
            if str(item.get("project") or project) != project:
                continue
            entity_id = _clip(item.get("source_id") or item.get("id") or item.get("eventId"), 180)
            if not entity_id:
                quarantine.append({"entity_type": entity_type, "entity_id": "", "reason": "missing_identity", "payload": {}})
                continue
            temporal = _temporal_fields(item)
            if temporal is None:
                quarantine.append({"entity_type": entity_type, "entity_id": entity_id, "reason": "invalid_temporal_interval", "payload": _safe_payload(item)})
                continue
            key = _node_key(project, entity_type, entity_id)
            source_sha = _sha256(_canonical_json(item))
            payload = _safe_payload(item)
            node = {"node_key": key, "entity_type": entity_type, "entity_id": entity_id, **temporal, "source_kind": entity_type, "source_id": entity_id, "source_sha256": source_sha, "payload": payload}
            node["node_sha256"] = _sha256(_canonical_json(node))
            nodes[key] = node
    return sorted(nodes.values(), key=lambda value: value["node_key"]), quarantine


def _collect_edges(project: str, links: Sequence[Mapping[str, Any]], nodes: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    node_keys = {str(item["node_key"]) for item in nodes}
    edges: dict[str, dict[str, Any]] = {}
    quarantine: list[dict[str, Any]] = []
    generated: list[dict[str, Any]] = [dict(item) for item in links]
    for node in nodes:
        payload = node.get("payload") if isinstance(node.get("payload"), Mapping) else {}
        supersedes = payload.get("supersedes")
        if supersedes:
            generated.append({"source_type": node["entity_type"], "source_id": node["entity_id"], "target_type": node["entity_type"], "target_id": supersedes, "relation": "supersedes", "project": project, "valid_from": node["valid_from"], "recorded_at": node["recorded_at"], "source": "metadata"})
    for raw in generated[:MEMORY_GRAPH_MAX_EDGES * 2]:
        item = dict(raw)
        if str(item.get("project") or project) != project:
            continue
        source_type = _clip(item.get("source_type") or item.get("source_entity_type"), 40) or "memory"
        target_type = _clip(item.get("target_type") or item.get("target_entity_type"), 40) or "memory"
        source_id = _clip(item.get("source_id"), 180)
        target_id = _clip(item.get("target_id"), 180)
        source_key = _node_key(project, source_type, source_id)
        target_key = _node_key(project, target_type, target_id)
        if not source_id or not target_id or source_key not in node_keys or target_key not in node_keys:
            quarantine.append({"entity_type": "edge", "entity_id": f"{source_id}->{target_id}", "reason": "unresolved_endpoint", "payload": _safe_payload(item)})
            continue
        temporal = _temporal_fields(item)
        if temporal is None:
            quarantine.append({"entity_type": "edge", "entity_id": f"{source_id}->{target_id}", "reason": "invalid_temporal_interval", "payload": _safe_payload(item)})
            continue
        relation = _clip(item.get("relation"), 80) or "related_to"
        source_sha = _sha256(_canonical_json(item))
        payload = _safe_payload(item)
        edge_key = _edge_key(source_key, target_key, relation, temporal["valid_from"])
        edge = {"edge_key": edge_key, "source_node_key": source_key, "target_node_key": target_key, "relation": relation, **temporal, "source_kind": _clip(item.get("source") or "memory-link", 80), "source_id": _clip(item.get("id") or edge_key, 180), "source_sha256": source_sha, "confidence": _bounded_score(item.get("confidence"), default=1.0), "payload": payload}
        edge["edge_sha256"] = _sha256(_canonical_json(edge))
        edges[edge_key] = edge
    return sorted(edges.values(), key=lambda value: value["edge_key"]), quarantine


def _read_nodes(connection: sqlite3.Connection, snapshot_id: str, project: str, as_of: str | None) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM memory_graph_nodes WHERE snapshot_id = ? AND project = ? ORDER BY node_key", (snapshot_id, project)).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        if as_of and not _active_at(str(row["valid_from"]), row["valid_until"], str(row["recorded_at"]), as_of):
            continue
        payload = json.loads(str(row["payload_json"]))
        result.append({"node_key": str(row["node_key"]), "entity_type": str(row["entity_type"]), "entity_id": str(row["entity_id"]), "valid_from": str(row["valid_from"]), "valid_until": row["valid_until"], "recorded_at": str(row["recorded_at"]), "payload": payload, "provenance": {"source_kind": str(row["source_kind"]), "source_id": str(row["source_id"]), "source_sha256": str(row["source_sha256"]), "node_sha256": str(row["node_sha256"]), "authority": "sqlite-authoritative"}})
    return result


def _read_edges(connection: sqlite3.Connection, snapshot_id: str, project: str, as_of: str | None) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM memory_graph_edges WHERE snapshot_id = ? AND project = ? ORDER BY edge_key", (snapshot_id, project)).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        if as_of and not _active_at(str(row["valid_from"]), row["valid_until"], str(row["recorded_at"]), as_of):
            continue
        result.append({"edge_key": str(row["edge_key"]), "source_node_key": str(row["source_node_key"]), "target_node_key": str(row["target_node_key"]), "relation": str(row["relation"]), "valid_from": str(row["valid_from"]), "valid_until": row["valid_until"], "recorded_at": str(row["recorded_at"]), "confidence": float(row["confidence"]), "payload": json.loads(str(row["payload_json"])), "provenance": {"source_kind": str(row["source_kind"]), "source_id": str(row["source_id"]), "source_sha256": str(row["source_sha256"]), "edge_sha256": str(row["edge_sha256"]), "authority": "sqlite-authoritative"}})
    return result


def _neighborhood(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], *, query: str, depth: int, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    roots = {item["node_key"] for item in nodes if query and (query in item["node_key"] or query.casefold() in str(item["entity_id"]).casefold())}
    if not roots and nodes:
        roots = {nodes[0]["node_key"]}
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge["source_node_key"], set()).add(edge["target_node_key"])
        adjacency.setdefault(edge["target_node_key"], set()).add(edge["source_node_key"])
    seen = set(roots)
    queue = deque((root, 0) for root in sorted(roots))
    while queue:
        current, distance = queue.popleft()
        if distance >= depth:
            continue
        for target in sorted(adjacency.get(current, set())):
            if target in seen:
                continue
            seen.add(target)
            queue.append((target, distance + 1))
    selected_nodes = [item for item in nodes if item["node_key"] in seen][:limit]
    selected_keys = {item["node_key"] for item in selected_nodes}
    selected_edges = [item for item in edges if item["source_node_key"] in selected_keys and item["target_node_key"] in selected_keys][:limit]
    return selected_nodes, selected_edges


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_graph_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            graph_digest TEXT NOT NULL,
            build_version TEXT NOT NULL,
            status TEXT NOT NULL,
            as_of TEXT,
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_graph_current (
            project TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_graph_nodes (
            snapshot_id TEXT NOT NULL,
            node_key TEXT NOT NULL,
            project TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            recorded_at TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            node_sha256 TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, node_key)
        );
        CREATE TABLE IF NOT EXISTS memory_graph_edges (
            snapshot_id TEXT NOT NULL,
            edge_key TEXT NOT NULL,
            project TEXT NOT NULL,
            source_node_key TEXT NOT NULL,
            target_node_key TEXT NOT NULL,
            relation TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            recorded_at TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            confidence REAL NOT NULL,
            payload_json TEXT NOT NULL,
            edge_sha256 TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, edge_key)
        );
        CREATE TABLE IF NOT EXISTS memory_graph_quarantine (
            snapshot_id TEXT NOT NULL,
            project TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_graph_nodes_project_time ON memory_graph_nodes(project, valid_from, valid_until);
        CREATE INDEX IF NOT EXISTS idx_memory_graph_edges_project_relation ON memory_graph_edges(project, relation, valid_from, valid_until);
        """
    )


def _connect_rw(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}")
    return connection


def _connect_ro(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS)
    except sqlite3.Error as exc:
        raise MemoryGraphError(f"memory graph database unavailable: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}")
    return connection


def _temporal_fields(item: Mapping[str, Any]) -> dict[str, str | None] | None:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    valid_from = _normalize_time(item.get("valid_from") or item.get("validFrom") or item.get("timestamp") or item.get("occurred_at") or item.get("occurredAt") or item.get("created_at") or item.get("createdAt") or "1970-01-01T00:00:00Z")
    valid_until_raw = item.get("valid_until") or item.get("validUntil") or metadata.get("valid_until") or metadata.get("validUntil")
    valid_until = _normalize_time(valid_until_raw) if valid_until_raw else None
    recorded_at = _normalize_time(item.get("recorded_at") or item.get("recordedAt") or item.get("updated_at") or item.get("updatedAt") or item.get("ingestedAt") or valid_from)
    if not valid_from or not recorded_at or (valid_until and valid_until <= valid_from):
        return None
    return {"valid_from": valid_from, "valid_until": valid_until, "recorded_at": recorded_at}


def _active_at(valid_from: str, valid_until: Any, recorded_at: str, as_of: str) -> bool:
    return valid_from <= as_of and recorded_at <= as_of and (not valid_until or str(valid_until) > as_of)


def _safe_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    return {
        "title": _clip(metadata.get("raw_title") or item.get("title"), 180),
        "type": _clip(item.get("memory_type") or item.get("type") or item.get("hookType") or item.get("relation"), 80),
        "tags": _bounded_strings(item.get("tags") or item.get("concepts") or metadata.get("tags"), 80, 12),
        "files": _bounded_strings(item.get("files") or metadata.get("files"), 240, 12),
        "supersedes": _clip(item.get("supersedes") or metadata.get("supersedes"), 180) or None,
        "source_refs": _bounded_strings(item.get("source_refs") or metadata.get("source_refs"), 240, 12),
    }


def _node_key(project: str, entity_type: str, entity_id: str) -> str:
    return f"{project}:{entity_type}:{entity_id}"


def _edge_key(source: str, target: str, relation: str, valid_from: str) -> str:
    return f"edge_{_sha256(f'{source}:{target}:{relation}:{valid_from}')[:32]}"


def _counts(items: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        name = str(item.get(key) or "unknown")
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def _estimate_tokens(value: Any) -> int:
    return max(1, (len(_canonical_json(value).encode("utf-8")) + 3) // 4)


def _bounded_strings(values: Any, limit: int, max_items: int) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    for value in values:
        text = _clip(value, limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= max_items:
            break
    return result


def _bounded_score(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if number != number:
        number = default
    return round(min(max(number, 0.0), 1.0), 4)


def _required_project(project: str) -> str:
    value = _clip(project, 120)
    if not value:
        raise MemoryGraphError("project is required")
    return value


def _normalize_time(value: Any) -> str | None:
    text = _clip(value, 64)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "MEMORY_GRAPH_BUILD_VERSION",
    "MEMORY_GRAPH_OPERATIONS",
    "MEMORY_GRAPH_SCHEMA_VERSION",
    "MemoryGraphError",
    "build_memory_graph",
    "explain_memory_graph",
    "query_memory_graph",
]
