"""Bounded, evidence-only trace and cross-service graph projections.

Runtime observations are useful corroboration for repository/code graphs, but
they are not an authority source.  This module deliberately returns a
read-only projection: no SQLite graph rows are written and no observed edge
can be promoted automatically.  Callers may expose the projection through a
REST/MCP read surface after applying their normal authentication boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Iterable, Mapping


TRACE_GRAPH_SCHEMA_VERSION = "bhm.trace-graph.v1"
TRACE_GRAPH_EVIDENCE_CLASS = "untrusted-runtime-observation"
TRACE_GRAPH_MAX_EVENTS = 256
TRACE_GRAPH_MAX_NODES = 128
TRACE_GRAPH_MAX_EDGES = 256
TRACE_GRAPH_MAX_TEXT = 240
TRACE_GRAPH_CONFIDENCE_CAP = 0.5


class TraceGraphError(ValueError):
    """Raised when a trace projection request violates bounded limits."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _clip(value: Any, limit: int = TRACE_GRAPH_MAX_TEXT) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _safe_name(value: Any) -> str:
    # Service identifiers are labels, not URLs or source snippets.  Reject
    # control characters and keep the projection stable across clients.
    text = _clip(value, 120)
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    # Service labels must never become a covert secret channel.  Preserve the
    # useful host/label while redacting URL credentials and common token
    # query fragments before the value reaches a graph projection.
    text = re.sub(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@", r"\1[REDACTED]@", text)
    text = re.sub(r"(?i)(token|secret|password|api[_-]?key)=([^&\s]+)", r"\1=[REDACTED]", text)
    return text.strip()


def _nested_maps(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for key in ("data", "metadata", "trace", "request", "context"):
        value = record.get(key)
        if isinstance(value, Mapping):
            result.append(value)
    # Flat records are accepted as a wire compatibility convenience.
    result.append(record)
    return result


def _first(mapping_list: Iterable[Mapping[str, Any]], keys: tuple[str, ...]) -> str:
    for mapping in mapping_list:
        for key in keys:
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                return _safe_name(value)
    return ""


def _trace_candidate(record: Mapping[str, Any]) -> dict[str, Any] | None:
    maps = _nested_maps(record)
    source = _first(maps, ("sourceService", "source_service", "callerService", "caller_service", "fromService", "from_service", "clientService", "client_service"))
    target = _first(maps, ("targetService", "target_service", "calleeService", "callee_service", "toService", "to_service", "serverService", "server_service"))
    if not source or not target or source == target:
        return None

    hook_type = _safe_name(record.get("hookType") or record.get("eventType") or record.get("event_type"))
    route = _first(maps, ("route", "routePath", "route_path", "path", "targetRoute", "target_route", "endpoint"))
    protocol = _first(maps, ("protocol", "transport", "rpc", "kind", "traceKind", "trace_kind"))
    # A pair of service names alone is insufficient: require a trace-ish
    # discriminator so ordinary memory observations cannot become edges.
    marker = f"{hook_type} {protocol} {route}".casefold()
    if not any(token in marker for token in ("trace", "http", "rpc", "request", "response", "route", "grpc", "graphql", "pubsub")):
        return None

    event_id = _safe_name(record.get("eventId") or record.get("id"))
    if not event_id:
        return None
    record_hash = _digest(record)
    occurred_at = _clip(record.get("timestamp") or record.get("occurredAt"), 80)
    project = _safe_name(record.get("project") or "")
    evidence = {
        "event_id": event_id,
        "record_sha256": record_hash,
        "project": project,
        "occurred_at": occurred_at,
        "source": _safe_name(record.get("source") or "observation-store"),
        "evidence_class": TRACE_GRAPH_EVIDENCE_CLASS,
        "trusted": False,
    }
    return {
        "source": source,
        "target": target,
        "route": route,
        "protocol": protocol,
        "hook_type": hook_type,
        "evidence": evidence,
    }


def build_trace_graph(
    observations: Iterable[Mapping[str, Any]],
    *,
    project: str | None = None,
    max_events: int = TRACE_GRAPH_MAX_EVENTS,
    max_nodes: int = TRACE_GRAPH_MAX_NODES,
    max_edges: int = TRACE_GRAPH_MAX_EDGES,
) -> dict[str, Any]:
    """Build a deterministic, bounded cross-service trace projection.

    Only explicit service-to-service observations with a trace/request marker
    are considered.  The returned edges are ``trace_observed`` evidence and
    are capped at confidence ``0.5``.  The function never writes or mutates
    the authoritative SQLite code graph.
    """

    for name, value, upper in (("max_events", max_events, TRACE_GRAPH_MAX_EVENTS), ("max_nodes", max_nodes, TRACE_GRAPH_MAX_NODES), ("max_edges", max_edges, TRACE_GRAPH_MAX_EDGES)):
        if not isinstance(value, int) or value < 1 or value > upper:
            raise TraceGraphError(f"{name} must be between 1 and {upper}")
    project_name = _safe_name(project)
    candidates: list[dict[str, Any]] = []
    inspected = 0
    skipped_project = 0
    skipped_invalid = 0
    for record in observations:
        if inspected >= max_events:
            break
        inspected += 1
        if not isinstance(record, Mapping):
            skipped_invalid += 1
            continue
        if project_name and _safe_name(record.get("project")) not in {project_name, ""}:
            skipped_project += 1
            continue
        if _safe_name(record.get("status")) == "purged":
            skipped_invalid += 1
            continue
        candidate = _trace_candidate(record)
        if candidate is not None:
            candidates.append(candidate)

    node_names = sorted({value for item in candidates for value in (item["source"], item["target"])})
    truncated = False
    if len(node_names) > max_nodes:
        node_names = node_names[:max_nodes]
        truncated = True
    allowed_nodes = set(node_names)
    edge_buckets: dict[str, dict[str, Any]] = {}
    for item in candidates:
        if item["source"] not in allowed_nodes or item["target"] not in allowed_nodes:
            truncated = True
            continue
        stable_key = f"trace_observed:{item['source']}:{item['target']}:{item['protocol']}:{item['route']}"
        bucket = edge_buckets.setdefault(
            stable_key,
            {
                "edge_id": "edge_trace_" + hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:24],
                "stable_key": stable_key,
                "edge_kind": "trace_observed",
                "source": item["source"],
                "target": item["target"],
                "protocol": item["protocol"],
                "route": item["route"],
                "confidence": TRACE_GRAPH_CONFIDENCE_CAP,
                "trusted": False,
                "promotion_status": "not-eligible",
                "evidence": [],
            },
        )
        if len(bucket["evidence"]) < 16:
            bucket["evidence"].append(item["evidence"])
    edges = [edge_buckets[key] for key in sorted(edge_buckets)[:max_edges]]
    if len(edge_buckets) > max_edges:
        truncated = True
    for edge in edges:
        edge["evidence"] = sorted(edge["evidence"], key=lambda item: str(item.get("event_id") or ""))
    node_ids = {
        name: "service_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
        for name in node_names
    }
    for edge in edges:
        edge["source_node_id"] = node_ids[edge["source"]]
        edge["target_node_id"] = node_ids[edge["target"]]
    nodes = [
        {
            "node_id": node_ids[name],
            "node_kind": "service",
            "name": name,
            "authority": "repository-code-graph-required",
            "trusted": False,
        }
        for name in node_names
    ]
    summary = {
        "observations_inspected": inspected,
        "trace_events": len(candidates),
        "nodes": len(nodes),
        "edges": len(edges),
        "protocols": dict(sorted(Counter(str(edge.get("protocol") or "unknown") for edge in edges).items())),
        "truncated": truncated,
        "skipped_project": skipped_project,
        "skipped_invalid": skipped_invalid,
    }
    material = {"schema_version": TRACE_GRAPH_SCHEMA_VERSION, "project": project_name, "nodes": nodes, "edges": edges, "summary": summary}
    return {
        **material,
        "graph_digest": _digest(material),
        "authority": "none",
        "evidence_class": TRACE_GRAPH_EVIDENCE_CLASS,
        "promotion": {
            "status": "not-eligible",
            "automatic": False,
            "reason": "runtime traces are corroboration only; explicit reviewer and code-graph digest are required",
            "required": ["reviewer", "matching_repository_snapshot", "matching_code_graph_digest", "explicit_apply_gate"],
        },
    }


def validate_trace_graph(graph: Mapping[str, Any]) -> dict[str, Any]:
    """Validate that a projection remains evidence-only and bounded."""

    errors: list[str] = []
    if graph.get("schema_version") != TRACE_GRAPH_SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    if graph.get("authority") != "none":
        errors.append("authority_must_be_none")
    if graph.get("evidence_class") != TRACE_GRAPH_EVIDENCE_CLASS:
        errors.append("evidence_class_mismatch")
    for edge in graph.get("edges") or []:
        if edge.get("edge_kind") != "trace_observed" or edge.get("trusted") is not False:
            errors.append("edge_not_untrusted")
        if float(edge.get("confidence") or 0) > TRACE_GRAPH_CONFIDENCE_CAP:
            errors.append("confidence_cap_exceeded")
        if not edge.get("evidence"):
            errors.append("edge_missing_evidence")
    return {"ok": not errors, "errors": sorted(set(errors)), "promotion_status": "not-eligible"}


__all__ = ["TRACE_GRAPH_SCHEMA_VERSION", "TRACE_GRAPH_EVIDENCE_CLASS", "TraceGraphError", "build_trace_graph", "validate_trace_graph"]
