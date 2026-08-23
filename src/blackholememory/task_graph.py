"""Durable task-governance graph for WI-07.

The graph is a rebuildable projection of BHM's existing task lifecycle inside
the same SQLite authority. Claims, leases, dependencies and evidence are
bounded, provenance-bearing records; no agent or model is started here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .filesystem_boundaries import assert_safe_path
from .resource_limits import SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS
from .task_dependencies import TaskDependencyDeclaration
from .task_dependencies import dependency_declarations_by_pair


TASK_GRAPH_SCHEMA_VERSION = "bhm.task-graph.v1"
TASK_GRAPH_BUILD_VERSION = "wi07-native-1"
TASK_GRAPH_OPERATIONS = ("status", "ready", "dependencies", "conflicts", "timeline")
TASK_GRAPH_MAX_NODES = 2_048
TASK_GRAPH_MAX_EDGES = 4_096


class TaskGraphError(ValueError):
    pass


def simulate_conflict_recovery_fixture(*, project: str = "fixture") -> dict[str, Any]:
    """Return a deterministic two-agent conflict/recovery trace."""

    steps = [
        {"step": 1, "event": "claim", "task_id": "task-main", "agent_id": "agent-a", "lease": "lease-a", "outcome": "accepted"},
        {"step": 2, "event": "claim", "task_id": "task-main", "agent_id": "agent-b", "lease": "lease-b", "outcome": "conflict"},
        {"step": 3, "event": "lease_expired", "task_id": "task-main", "agent_id": "agent-a", "lease": "lease-a", "outcome": "released"},
        {"step": 4, "event": "claim", "task_id": "task-main", "agent_id": "agent-b", "lease": "lease-b", "outcome": "recovered"},
        {"step": 5, "event": "evidence", "task_id": "task-main", "agent_id": "agent-b", "evidence_id": "evidence-tests", "outcome": "accepted"},
        {"step": 6, "event": "close", "task_id": "task-main", "agent_id": "agent-b", "outcome": "evidence_backed"},
    ]
    return {
        "schema_version": TASK_GRAPH_SCHEMA_VERSION,
        "project": _clip(project, 120) or "fixture",
        "fixture_id": "two-agent-conflict-recovery-v1",
        "steps": steps,
        "final": {"task_id": "task-main", "status": "closed", "owner": "agent-b", "evidence_backed": True, "conflict_recovered": True},
        "execution": {"agents_started": False, "writes_sqlite": False, "model_started": False, "auto_apply": False},
    }


def build_task_graph(
    database_path: Path | str,
    *,
    project: str,
    tasks: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]] = (),
    evidence: Sequence[Mapping[str, Any]] = (),
    events: Sequence[Mapping[str, Any]] = (),
    as_of: str | None = None,
    fail_after_stage: str | None = None,
    source_kind: str = "task",
    dependency_declarations: Sequence[Mapping[str, Any] | TaskDependencyDeclaration] = (),
    connection: sqlite3.Connection | None = None,
    publish: bool = True,
    summary_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and publish one bounded task-graph snapshot.

    ``connection`` is intentionally optional.  The default keeps the existing
    self-contained transaction behaviour; a bounded importer can provide one
    authoritative transaction so its companion rows and the graph publication
    either commit together or roll back together.
    """
    project_name = _required_project(project)
    normalized_source_kind = _clip(source_kind, 80) or "task"
    effective_as_of = _normalize_time(as_of) if as_of else _utc_now()
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    quarantine: list[dict[str, Any]] = []
    task_map: dict[str, dict[str, Any]] = {}
    for raw in list(tasks)[:TASK_GRAPH_MAX_NODES]:
        item = dict(raw)
        if str(item.get("project") or project_name) != project_name:
            continue
        task_id = _clip(item.get("task_id") or item.get("id"), 180)
        if not task_id:
            quarantine.append({"kind": "task", "id": "", "reason": "missing_identity"})
            continue
        temporal = _temporal(item)
        if temporal is None:
            quarantine.append({"kind": "task", "id": task_id, "reason": "invalid_temporal_interval"})
            continue
        key = _node_key(project_name, "task", task_id)
        dependencies = _bounded_strings(item.get("dependencies") or item.get("depends_on") or item.get("blocked_by"), 180, 32)
        payload = {"title": _clip(item.get("title"), 180), "status": _clip(item.get("status"), 40) or "open", "owner": _clip(item.get("owner") or item.get("owner_id"), 100), "dependencies": dependencies, "intent": _clip(item.get("intent"), 320), "evidence_backed": bool(item.get("evidence_backed"))}
        node = _node(key, "task", task_id, temporal, normalized_source_kind, task_id, payload, item)
        nodes[key] = node
        task_map[task_id] = node
    declared_dependencies = dependency_declarations_by_pair(
        dependency_declarations,
        project=project_name,
        known_task_ids=set(task_map),
    )
    for (task_id, dependency_id), declaration in declared_dependencies.items():
        dependencies = task_map[task_id]["payload"]["dependencies"]
        if dependency_id not in dependencies:
            dependencies.append(dependency_id)
    for task_id, node in sorted(task_map.items()):
        for dependency in node["payload"].get("dependencies") or []:
            target_key = _node_key(project_name, "task", dependency)
            if target_key not in nodes:
                quarantine.append({"kind": "edge", "id": f"{task_id}->{dependency}", "reason": "unresolved_endpoint"})
                continue
            declaration = declared_dependencies.get((task_id, dependency))
            dependency_source_kind = "task_dependency_declaration" if declaration is not None else normalized_source_kind
            dependency_source_id = declaration.digest() if declaration is not None else f"{task_id}->{dependency}"
            dependency_payload = {"dependency": dependency}
            if declaration is not None:
                dependency_payload.update({
                    "declaration_digest": declaration.digest(),
                    "declared_by": declaration.declared_by,
                    "declared_at": declaration.declared_at,
                })
            _add_edge(edges, _edge(project_name, node, nodes[target_key], "depends_on", dependency_source_kind, dependency_source_id, node["valid_from"], dependency_payload))
    active_claims: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in list(claims)[:TASK_GRAPH_MAX_EDGES]:
        item = dict(raw)
        if str(item.get("project") or project_name) != project_name:
            continue
        task_id = _clip(item.get("task_id") or item.get("target_id"), 180)
        agent_id = _clip(item.get("agent_id") or item.get("owner_id"), 100)
        claim_id = _clip(item.get("claim_id") or item.get("id"), 180) or f"claim:{task_id}:{agent_id}"
        if _node_key(project_name, "task", task_id) not in nodes or not agent_id:
            quarantine.append({"kind": "claim", "id": claim_id, "reason": "unresolved_endpoint"})
            continue
        temporal = _temporal(item)
        if temporal is None:
            quarantine.append({"kind": "claim", "id": claim_id, "reason": "invalid_temporal_interval"})
            continue
        agent_key = _node_key(project_name, "agent", agent_id)
        if agent_key not in nodes:
            nodes[agent_key] = _node(agent_key, "agent", agent_id, temporal, "claim", claim_id, {"agent_id": agent_id}, item)
        expires_at = _normalize_time(item.get("expires_at") or item.get("expiresAt"))
        expired = bool(expires_at and expires_at <= effective_as_of)
        claim_payload = {"claim_id": claim_id, "lease_id": _clip(item.get("lease_id") or item.get("leaseId"), 180), "task_id": task_id, "agent_id": agent_id, "expires_at": expires_at, "expired": expired, "status": _clip(item.get("status"), 40) or ("expired" if expired else "active")}
        edge = _edge(project_name, nodes[agent_key], nodes[_node_key(project_name, "task", task_id)], "claims", "claim", claim_id, temporal["valid_from"], claim_payload, confidence=_bounded_score(item.get("confidence"), default=1.0))
        _add_edge(edges, edge)
        if not expired and claim_payload["status"] not in {"released", "expired", "rejected"}:
            active_claims[task_id].append({"agent_id": agent_id, "claim_id": claim_id, "edge": edge})
    conflict_count = 0
    for task_id, claim_items in sorted(active_claims.items()):
        for left in claim_items:
            for right in claim_items:
                if left["agent_id"] >= right["agent_id"]:
                    continue
                conflict_count += 1
                _add_edge(edges, _edge(project_name, nodes[_node_key(project_name, "agent", left["agent_id"])], nodes[_node_key(project_name, "agent", right["agent_id"])], "conflicts", "governance", f"{left['claim_id']}:{right['claim_id']}", left["edge"]["valid_from"], {"task_id": task_id, "left_claim": left["claim_id"], "right_claim": right["claim_id"]}, confidence=1.0))
    evidence_by_task: dict[str, int] = defaultdict(int)
    for raw in list(evidence)[:TASK_GRAPH_MAX_EDGES]:
        item = dict(raw)
        if str(item.get("project") or project_name) != project_name:
            continue
        task_id = _clip(item.get("task_id") or item.get("target_id"), 180)
        evidence_id = _clip(item.get("evidence_id") or item.get("id"), 180)
        if _node_key(project_name, "task", task_id) not in nodes or not evidence_id:
            quarantine.append({"kind": "evidence", "id": evidence_id, "reason": "unresolved_endpoint"})
            continue
        temporal = _temporal(item)
        if temporal is None:
            quarantine.append({"kind": "evidence", "id": evidence_id, "reason": "invalid_temporal_interval"})
            continue
        evidence_key = _node_key(project_name, "evidence", evidence_id)
        payload = {"kind": _clip(item.get("kind"), 80), "status": _clip(item.get("status"), 40) or "unknown", "digest": _clip(item.get("digest") or item.get("sha256"), 64), "path": _clip(item.get("path"), 240)}
        nodes[evidence_key] = _node(evidence_key, "evidence", evidence_id, temporal, "evidence", evidence_id, payload, item)
        _add_edge(edges, _edge(project_name, nodes[evidence_key], nodes[_node_key(project_name, "task", task_id)], "evidence_for", "evidence", evidence_id, temporal["valid_from"], payload))
        if payload["status"] in {"accepted", "passed", "complete", "green"}:
            evidence_by_task[task_id] += 1
    for raw in list(events)[:TASK_GRAPH_MAX_EDGES]:
        item = dict(raw)
        if str(item.get("project") or project_name) != project_name:
            continue
        task_id = _clip(item.get("task_id") or item.get("target_id"), 180)
        event_id = _clip(item.get("event_id") or item.get("id"), 180)
        if _node_key(project_name, "task", task_id) not in nodes or not event_id:
            quarantine.append({"kind": "event", "id": event_id, "reason": "unresolved_endpoint"})
            continue
        temporal = _temporal(item)
        if temporal is None:
            quarantine.append({"kind": "event", "id": event_id, "reason": "invalid_temporal_interval"})
            continue
        event_key = _node_key(project_name, "event", event_id)
        nodes[event_key] = _node(event_key, "event", event_id, temporal, "event", event_id, {"kind": _clip(item.get("kind") or item.get("event"), 80), "outcome": _clip(item.get("outcome"), 120)}, item)
        _add_edge(edges, _edge(project_name, nodes[event_key], nodes[_node_key(project_name, "task", task_id)], "observed_for", "event", event_id, temporal["valid_from"], nodes[event_key]["payload"]))
    cycle_nodes = _find_cycles(nodes, edges)
    for cycle in cycle_nodes:
        quarantine.append({"kind": "dependency", "id": cycle, "reason": "dependency_cycle"})
    for task_id, node in task_map.items():
        status = node["payload"].get("status")
        deps = node["payload"].get("dependencies") or []
        deps_closed = all(str(task_map.get(dep, {}).get("payload", {}).get("status")) in {"closed", "complete", "done"} for dep in deps) if deps else True
        active_conflict = any(edge["relation"] == "conflicts" and task_id in str(edge["payload"].get("task_id")) for edge in edges.values())
        node["payload"]["ready"] = status in {"open", "ready", "pending"} and deps_closed and not active_conflict and task_id not in cycle_nodes
        node["payload"]["evidence_backed_close"] = status in {"closed", "complete", "done"} and evidence_by_task.get(task_id, 0) > 0
    nodes_list = sorted(nodes.values(), key=lambda item: item["node_key"])
    edges_list = sorted(edges.values(), key=lambda item: item["edge_key"])
    graph_digest = _sha256(_canonical_json({"nodes": nodes_list, "edges": edges_list, "quarantine": quarantine}))
    snapshot_id = f"tgraph_{_sha256(f'{project_name}:{graph_digest}')[:24]}"
    summary = {"node_count": len(nodes_list), "edge_count": len(edges_list), "quarantine_count": len(quarantine), "conflict_count": conflict_count, "lease_expired_count": sum(1 for edge in edges_list if edge["relation"] == "claims" and edge["payload"].get("expired")), "ready_count": sum(1 for node in nodes_list if node["entity_type"] == "task" and node["payload"].get("ready")), "evidence_backed_close_count": sum(1 for node in nodes_list if node["entity_type"] == "task" and node["payload"].get("evidence_backed_close"))}
    if summary_extra:
        summary["source_contract"] = dict(summary_extra)
    if len(nodes_list) > TASK_GRAPH_MAX_NODES or len(edges_list) > TASK_GRAPH_MAX_EDGES:
        raise TaskGraphError("task graph bounds exceeded")
    path = assert_safe_path(Path(database_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_safe_path(path.parent, reject_hardlink_target=False)
    assert_safe_path(path)
    owns_connection = connection is None
    active_connection = connection if connection is not None else _connect_rw(path)
    if active_connection is None:
        raise TaskGraphError("task graph database unavailable")
    active_connection.row_factory = sqlite3.Row
    now = _utc_now()
    try:
        _initialize_schema(active_connection)
        if owns_connection:
            active_connection.execute("BEGIN IMMEDIATE")
        else:
            active_connection.execute("SAVEPOINT task_graph_build")
        active_connection.execute("DELETE FROM task_graph_nodes WHERE snapshot_id = ?", (snapshot_id,))
        active_connection.execute("DELETE FROM task_graph_edges WHERE snapshot_id = ?", (snapshot_id,))
        active_connection.execute("DELETE FROM task_graph_quarantine WHERE snapshot_id = ?", (snapshot_id,))
        active_connection.execute("DELETE FROM task_graph_snapshots WHERE snapshot_id = ?", (snapshot_id,))
        active_connection.execute("INSERT INTO task_graph_snapshots(snapshot_id,project,graph_digest,build_version,status,as_of,summary_json,created_at) VALUES (?,?,?,?, 'building',?,?,?)", (snapshot_id, project_name, graph_digest, TASK_GRAPH_BUILD_VERSION, effective_as_of, _canonical_json(summary), now))
        for node in nodes_list:
            active_connection.execute("INSERT INTO task_graph_nodes(snapshot_id,node_key,project,entity_type,entity_id,valid_from,valid_until,recorded_at,source_kind,source_id,source_sha256,payload_json,node_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (snapshot_id, node["node_key"], project_name, node["entity_type"], node["entity_id"], node["valid_from"], node["valid_until"], node["recorded_at"], node["source_kind"], node["source_id"], node["source_sha256"], _canonical_json(node["payload"]), node["node_sha256"]))
        for edge in edges_list:
            active_connection.execute("INSERT INTO task_graph_edges(snapshot_id,edge_key,project,source_node_key,target_node_key,relation,valid_from,valid_until,recorded_at,source_kind,source_id,source_sha256,confidence,payload_json,edge_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (snapshot_id, edge["edge_key"], project_name, edge["source_node_key"], edge["target_node_key"], edge["relation"], edge["valid_from"], edge["valid_until"], edge["recorded_at"], edge["source_kind"], edge["source_id"], edge["source_sha256"], edge["confidence"], _canonical_json(edge["payload"]), edge["edge_sha256"]))
        for item in quarantine:
            active_connection.execute("INSERT INTO task_graph_quarantine(snapshot_id,project,kind,entity_id,reason,payload_json,created_at) VALUES (?,?,?,?,?,?,?)", (snapshot_id, project_name, item.get("kind", ""), item.get("id", ""), item.get("reason", "unknown"), _canonical_json(item), now))
        if fail_after_stage == "before_publish":
            raise TaskGraphError("injected publish failure")
        if publish:
            active_connection.execute("UPDATE task_graph_snapshots SET status = 'published' WHERE snapshot_id = ?", (snapshot_id,))
            active_connection.execute("INSERT INTO task_graph_current(project,snapshot_id,updated_at) VALUES (?,?,?) ON CONFLICT(project) DO UPDATE SET snapshot_id=excluded.snapshot_id, updated_at=excluded.updated_at", (project_name, snapshot_id, now))
        else:
            active_connection.execute("UPDATE task_graph_snapshots SET status = 'staged' WHERE snapshot_id = ?", (snapshot_id,))
        if owns_connection:
            active_connection.commit()
        else:
            active_connection.execute("RELEASE SAVEPOINT task_graph_build")
    except Exception:
        if owns_connection:
            active_connection.rollback()
        else:
            active_connection.execute("ROLLBACK TO SAVEPOINT task_graph_build")
            active_connection.execute("RELEASE SAVEPOINT task_graph_build")
        raise
    finally:
        if owns_connection:
            active_connection.close()
    return {"schema_version": TASK_GRAPH_SCHEMA_VERSION, "ok": True, "action": "build", "publication": "published" if publish else "staged", "project": project_name, "snapshot_id": snapshot_id, "graph_digest": graph_digest, "summary": summary, "quarantine": quarantine, "execution": {"writes_sqlite": True, "writes_qdrant": False, "writes_mem0": False, "agents_started": False, "model_started": False, "public_mcp": False}}


def query_task_graph(database_path: Path | str, *, project: str, operation: str = "status", query: str = "", snapshot_id: str | None = None, limit: int = 64, max_tokens: int = 4_096, time_budget_ms: float = 500.0) -> dict[str, Any]:
    if operation not in TASK_GRAPH_OPERATIONS:
        raise TaskGraphError(f"unsupported operation: {operation}")
    if not 1 <= int(limit) <= 128 or not 128 <= int(max_tokens) <= 16_384 or not 0 < float(time_budget_ms) <= 5_000:
        raise TaskGraphError("query bounds exceeded")
    project_name = _required_project(project)
    path = assert_safe_path(Path(database_path))
    if not path.exists():
        raise TaskGraphError("task graph database unavailable")
    started = time.perf_counter()
    connection = _connect_ro(path)
    try:
        current = connection.execute("SELECT snapshot_id FROM task_graph_current WHERE project = ?", (project_name,)).fetchone()
        selected = str(snapshot_id or (current[0] if current else ""))
        if not selected:
            raise TaskGraphError("task graph snapshot unavailable")
        snapshot = connection.execute("SELECT * FROM task_graph_snapshots WHERE snapshot_id = ? AND project = ? AND status = 'published'", (selected, project_name)).fetchone()
        if snapshot is None:
            raise TaskGraphError("task graph snapshot unavailable")
        nodes = _read_nodes(connection, selected, project_name)
        edges = _read_edges(connection, selected, project_name)
        if operation == "ready":
            nodes = [item for item in nodes if item["entity_type"] == "task" and item["payload"].get("ready")][: int(limit)]
        elif operation == "dependencies":
            nodes, edges = _dependency_subgraph(nodes, edges, query, int(limit))
        elif operation == "conflicts":
            edges = [item for item in edges if item["relation"] == "conflicts" and (not query or query in str(item["payload"]))][: int(limit)]
            keys = {item["source_node_key"] for item in edges} | {item["target_node_key"] for item in edges}
            nodes = [item for item in nodes if item["node_key"] in keys][: int(limit)]
        elif operation == "timeline":
            nodes = sorted([item for item in nodes if item["entity_type"] == "event"], key=lambda item: (item["recorded_at"], item["node_key"]))[: int(limit)]
            keys = {item["node_key"] for item in nodes}
            edges = [item for item in edges if item["source_node_key"] in keys or item["target_node_key"] in keys][: int(limit)]
        else:
            nodes = nodes[: int(limit)]
            edges = edges[: int(limit)]
        payload = {"project": project_name, "snapshot_id": selected, "graph_digest": str(snapshot["graph_digest"]), "operation": operation, "nodes": nodes, "edges": edges}
        estimated = _estimate_tokens(payload)
        omissions: list[str] = []
        while estimated > max_tokens and (payload["edges"] or payload["nodes"]):
            if payload["edges"]:
                payload["edges"].pop()
            else:
                payload["nodes"].pop()
            omissions.append("items_truncated_to_token_budget")
            estimated = _estimate_tokens(payload)
        elapsed = (time.perf_counter() - started) * 1000.0
        core = {**payload, "estimated_tokens": estimated, "omissions": sorted(set(omissions))}
        return {"schema_version": TASK_GRAPH_SCHEMA_VERSION, "ok": True, "response_digest": _sha256(_canonical_json(core)), **core, "summary": json.loads(str(snapshot["summary_json"])), "provenance": {"complete": all(item.get("provenance") for item in payload["nodes"] + payload["edges"]), "authority": "sqlite-authoritative", "snapshot_id": selected, "graph_digest": str(snapshot["graph_digest"])}, "budget": {"max_tokens": int(max_tokens), "estimated_tokens": estimated, "elapsed_ms": round(elapsed, 3), "within_time_budget": elapsed <= float(time_budget_ms)}, "execution": {"writes_sqlite": False, "writes_qdrant": False, "writes_mem0": False, "agents_started": False, "model_started": False, "raw_source_returned": False, "public_mcp": False}}
    finally:
        connection.close()


def explain_task_graph(database_path: Path | str, **kwargs: Any) -> dict[str, Any]:
    result = query_task_graph(database_path, **kwargs)
    result["explain"] = {"operation": result.get("operation"), "reason_codes": ["dependency_and_lease_rules_applied", "evidence_gate_visible", "bounded_read_only"]}
    return result


def _node(key: str, entity_type: str, entity_id: str, temporal: Mapping[str, Any], source_kind: str, source_id: str, payload: Mapping[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
    source_sha = _sha256(_canonical_json(raw))
    result = {"node_key": key, "entity_type": entity_type, "entity_id": entity_id, "valid_from": temporal["valid_from"], "valid_until": temporal["valid_until"], "recorded_at": temporal["recorded_at"], "source_kind": source_kind, "source_id": source_id, "source_sha256": source_sha, "payload": dict(payload)}
    result["node_sha256"] = _sha256(_canonical_json(result))
    result["provenance"] = {"source_kind": source_kind, "source_id": source_id, "source_sha256": source_sha, "authority": "sqlite-authoritative"}
    return result


def _edge(project: str, source: Mapping[str, Any], target: Mapping[str, Any], relation: str, source_kind: str, source_id: str, valid_from: str, payload: Mapping[str, Any], confidence: float = 1.0) -> dict[str, Any]:
    edge_key = f"edge_{_sha256(f'{source['node_key']}:{target['node_key']}:{relation}:{valid_from}')[:32]}"
    raw = {"source": source_id, "relation": relation, "payload": dict(payload)}
    source_sha = _sha256(_canonical_json(raw))
    edge = {"edge_key": edge_key, "source_node_key": source["node_key"], "target_node_key": target["node_key"], "relation": relation, "valid_from": valid_from, "valid_until": None, "recorded_at": source.get("recorded_at") or valid_from, "source_kind": source_kind, "source_id": source_id, "source_sha256": source_sha, "confidence": round(min(max(float(confidence), 0.0), 1.0), 4), "payload": dict(payload)}
    edge["edge_sha256"] = _sha256(_canonical_json(edge))
    edge["provenance"] = {"source_kind": source_kind, "source_id": source_id, "source_sha256": source_sha, "authority": "sqlite-authoritative"}
    return edge


def _add_edge(edges: dict[str, dict[str, Any]], edge: dict[str, Any]) -> None:
    edges[str(edge["edge_key"])] = edge


def _find_cycles(nodes: Mapping[str, Mapping[str, Any]], edges: Mapping[str, Mapping[str, Any]]) -> set[str]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges.values():
        if edge["relation"] == "depends_on":
            adjacency[str(edge["source_node_key"])].add(str(edge["target_node_key"]))
    cycles: set[str] = set()
    def visit(node: str, path: list[str], active: set[str]) -> None:
        if node in active:
            cycles.add(node.split(":", 2)[-1])
            return
        if node in path:
            return
        active.add(node)
        for target in adjacency.get(node, set()):
            visit(target, path + [node], active)
        active.remove(node)
    for key in nodes:
        visit(key, [], set())
    return cycles


def _dependency_subgraph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], query: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    roots = {item["node_key"] for item in nodes if query and (query in item["node_key"] or query in item["entity_id"])}
    if not roots and nodes:
        roots = {nodes[0]["node_key"]}
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge["relation"] == "depends_on":
            adjacency[edge["source_node_key"]].add(edge["target_node_key"])
    seen = set(roots)
    queue = deque(sorted(roots))
    while queue and len(seen) < limit:
        current = queue.popleft()
        for target in sorted(adjacency.get(current, set())):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    selected = [item for item in nodes if item["node_key"] in seen][:limit]
    keys = {item["node_key"] for item in selected}
    return selected, [edge for edge in edges if edge["source_node_key"] in keys and edge["target_node_key"] in keys][:limit]


def _read_nodes(connection: sqlite3.Connection, snapshot_id: str, project: str) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM task_graph_nodes WHERE snapshot_id = ? AND project = ? ORDER BY node_key", (snapshot_id, project)).fetchall()
    return [{"node_key": str(row["node_key"]), "entity_type": str(row["entity_type"]), "entity_id": str(row["entity_id"]), "valid_from": str(row["valid_from"]), "valid_until": row["valid_until"], "recorded_at": str(row["recorded_at"]), "payload": json.loads(str(row["payload_json"])), "provenance": {"source_kind": str(row["source_kind"]), "source_id": str(row["source_id"]), "source_sha256": str(row["source_sha256"]), "node_sha256": str(row["node_sha256"]), "authority": "sqlite-authoritative"}} for row in rows]


def _read_edges(connection: sqlite3.Connection, snapshot_id: str, project: str) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM task_graph_edges WHERE snapshot_id = ? AND project = ? ORDER BY edge_key", (snapshot_id, project)).fetchall()
    return [{"edge_key": str(row["edge_key"]), "source_node_key": str(row["source_node_key"]), "target_node_key": str(row["target_node_key"]), "relation": str(row["relation"]), "valid_from": str(row["valid_from"]), "valid_until": row["valid_until"], "recorded_at": str(row["recorded_at"]), "confidence": float(row["confidence"]), "payload": json.loads(str(row["payload_json"])), "provenance": {"source_kind": str(row["source_kind"]), "source_id": str(row["source_id"]), "source_sha256": str(row["source_sha256"]), "edge_sha256": str(row["edge_sha256"]), "authority": "sqlite-authoritative"}} for row in rows]


def _initialize_schema(connection: sqlite3.Connection) -> None:
    # Do not use executescript here: sqlite3 executescript() commits any
    # pending transaction before running the script, which would break the
    # caller-owned atomic sidecar/import transaction.  Individual idempotent
    # statements preserve the caller's transaction and SAVEPOINT semantics.
    statements = (
        "CREATE TABLE IF NOT EXISTS task_graph_snapshots(snapshot_id TEXT PRIMARY KEY, project TEXT NOT NULL, graph_digest TEXT NOT NULL, build_version TEXT NOT NULL, status TEXT NOT NULL, as_of TEXT, summary_json TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS task_graph_current(project TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS task_graph_nodes(snapshot_id TEXT NOT NULL, node_key TEXT NOT NULL, project TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, valid_from TEXT NOT NULL, valid_until TEXT, recorded_at TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, node_sha256 TEXT NOT NULL, PRIMARY KEY(snapshot_id,node_key))",
        "CREATE TABLE IF NOT EXISTS task_graph_edges(snapshot_id TEXT NOT NULL, edge_key TEXT NOT NULL, project TEXT NOT NULL, source_node_key TEXT NOT NULL, target_node_key TEXT NOT NULL, relation TEXT NOT NULL, valid_from TEXT NOT NULL, valid_until TEXT, recorded_at TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT NOT NULL, source_sha256 TEXT NOT NULL, confidence REAL NOT NULL, payload_json TEXT NOT NULL, edge_sha256 TEXT NOT NULL, PRIMARY KEY(snapshot_id,edge_key))",
        "CREATE TABLE IF NOT EXISTS task_graph_quarantine(snapshot_id TEXT NOT NULL, project TEXT NOT NULL, kind TEXT NOT NULL, entity_id TEXT NOT NULL, reason TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_task_graph_nodes_project ON task_graph_nodes(project,entity_type,entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_task_graph_edges_relation ON task_graph_edges(project,relation)",
    )
    for statement in statements:
        connection.execute(statement)


def _connect_rw(path: Path) -> sqlite3.Connection:
    assert_safe_path(path)
    connection = sqlite3.connect(str(path), timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}")
    return connection


def _connect_ro(path: Path) -> sqlite3.Connection:
    assert_safe_path(path)
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS)
    except sqlite3.Error as exc:
        raise TaskGraphError(f"task graph database unavailable: {exc}") from exc
    connection.row_factory = sqlite3.Row
    return connection


def _temporal(item: Mapping[str, Any]) -> dict[str, str | None] | None:
    valid_from = _normalize_time(item.get("valid_from") or item.get("validFrom") or item.get("created_at") or item.get("createdAt") or item.get("timestamp") or "1970-01-01T00:00:00Z")
    valid_until = _normalize_time(item.get("valid_until") or item.get("validUntil"))
    recorded_at = _normalize_time(item.get("recorded_at") or item.get("recordedAt") or item.get("updated_at") or item.get("updatedAt") or valid_from)
    if not valid_from or not recorded_at or (valid_until and valid_until <= valid_from):
        return None
    return {"valid_from": valid_from, "valid_until": valid_until, "recorded_at": recorded_at}


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


def _active_time(value: str) -> str:
    return value


def _node_key(project: str, entity_type: str, entity_id: str) -> str:
    return f"{project}:{entity_type}:{entity_id}"


def _edge_key(source: str, target: str, relation: str, valid_from: str) -> str:
    return f"edge_{_sha256(f'{source}:{target}:{relation}:{valid_from}')[:32]}"


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
    return round(min(max(number, 0.0), 1.0), 4)


def _required_project(project: str) -> str:
    value = _clip(project, 120)
    if not value:
        raise TaskGraphError("project is required")
    return value


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["TASK_GRAPH_BUILD_VERSION", "TASK_GRAPH_OPERATIONS", "TASK_GRAPH_SCHEMA_VERSION", "TaskGraphError", "build_task_graph", "explain_task_graph", "query_task_graph", "simulate_conflict_recovery_fixture"]
