"""Clean-room, read-only graph query subset for CBM parity.

This deliberately implements a small, explicit pattern language rather than
embedding a general Cypher engine.  The input is compiled to an in-memory
bounded traversal over a published SQLite graph snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Mapping

from .code_graph import CODE_GRAPH_SCHEMA_VERSION, SQLiteCodeGraphStore
from .code_graph_query import _edge_view, _node_view
from .graph_query_quality_receipt import build_graph_query_quality_receipt


CODE_GRAPH_DSL_SCHEMA_VERSION = "bhm.code-graph.dsl.v4"
MAX_DSL_QUERY_CHARS = 1_200
MAX_DSL_ROWS = 128
MAX_DSL_PATHS = 256
MAX_DSL_OFFSET = 10_000
MAX_DSL_TIME_BUDGET_MS = 5_000.0
MAX_DSL_REGEX_CHARS = 160
ALLOWED_RETURN_FIELDS = frozenset({"node_id", "stable_key", "kind", "node_kind", "name", "qualified_name", "path", "language", "signature"})
ALLOWED_OPERATORS = frozenset({"=", "=~", "contains"})
FORBIDDEN_TOKENS = re.compile(r"\b(?:CREATE|DELETE|DETACH|DROP|MERGE|REMOVE|SET|LOAD|CALL|UNION|USE|FOREACH|PERIODIC)\b|;|//|/\*|\*/", re.IGNORECASE)


class GraphDslError(ValueError):
    """Raised when a graph DSL query is unsafe or unsupported."""


_PATTERN_RE = re.compile(
    r"^\s*MATCH\s+\((?P<left_alias>[A-Za-z_][A-Za-z0-9_]*)(?::(?P<left_label>[A-Za-z_][A-Za-z0-9_]*))?\)"
    r"\s*-\s*\[:(?P<edge>[A-Za-z_][A-Za-z0-9_]*)\]\s*->\s*"
    r"\((?P<right_alias>[A-Za-z_][A-Za-z0-9_]*)(?::(?P<right_label>[A-Za-z_][A-Za-z0-9_]*))?\)"
    r"(?:\s+WHERE\s+(?P<where_alias>[A-Za-z_][A-Za-z0-9_]*)\.(?P<where_field>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?P<where_operator>=~|=|(?i:CONTAINS))\s*(?P<where_value>'[^']{0,480}'|\"[^\"]{0,480}\"))?"
    r"\s+RETURN\s+(?P<returns>(?:[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*|(?i:COUNT)\([A-Za-z_][A-Za-z0-9_]*\)(?:\s+(?i:AS)\s+[A-Za-z_][A-Za-z0-9_]*)?)(?:\s*,\s*(?:[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*|(?i:COUNT)\([A-Za-z_][A-Za-z0-9_]*\)(?:\s+(?i:AS)\s+[A-Za-z_][A-Za-z0-9_]*)?))*)"
    r"(?:\s+(?i:GROUP)\s+(?i:BY)\s+(?P<group_alias>[A-Za-z_][A-Za-z0-9_]*)\.(?P<group_field>[A-Za-z_][A-Za-z0-9_]*))?"
    r"(?:\s+LIMIT\s+(?P<query_limit>\d+))?\s*$",
    re.IGNORECASE,
)

_TWO_HOP_PATTERN_RE = re.compile(
    r"^\s*MATCH\s+\((?P<left_alias>[A-Za-z_][A-Za-z0-9_]*)(?::(?P<left_label>[A-Za-z_][A-Za-z0-9_]*))?\)"
    r"\s*-\s*\[:(?P<edge_one>[A-Za-z_][A-Za-z0-9_]*)\]\s*->\s*"
    r"\((?P<middle_alias>[A-Za-z_][A-Za-z0-9_]*)(?::(?P<middle_label>[A-Za-z_][A-Za-z0-9_]*))?\)"
    r"\s*-\s*\[:(?P<edge_two>[A-Za-z_][A-Za-z0-9_]*)\]\s*->\s*"
    r"\((?P<right_alias>[A-Za-z_][A-Za-z0-9_]*)(?::(?P<right_label>[A-Za-z_][A-Za-z0-9_]*))?\)"
    r"(?:\s+WHERE\s+(?P<where_alias>[A-Za-z_][A-Za-z0-9_]*)\.(?P<where_field>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?P<where_operator>=~|=|(?i:CONTAINS))\s*(?P<where_value>'[^']{0,480}'|\"[^\"]{0,480}\"))?"
    r"\s+RETURN\s+(?P<returns>(?:[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*|(?i:COUNT)\([A-Za-z_][A-Za-z0-9_]*\)(?:\s+(?i:AS)\s+[A-Za-z_][A-Za-z0-9_]*)?)(?:\s*,\s*(?:[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*|(?i:COUNT)\([A-Za-z_][A-Za-z0-9_]*\)(?:\s+(?i:AS)\s+[A-Za-z_][A-Za-z0-9_]*)?))*)"
    r"(?:\s+(?i:GROUP)\s+(?i:BY)\s+(?P<group_alias>[A-Za-z_][A-Za-z0-9_]*)\.(?P<group_field>[A-Za-z_][A-Za-z0-9_]*))?"
    r"(?:\s+LIMIT\s+(?P<query_limit>\d+))?\s*$",
    re.IGNORECASE,
)

_NESTED_REGEX_QUANTIFIER_RE = re.compile(
    r"\([^()\n]{0,120}(?:[+*]|\{\d+(?:,\d*)?\})[^()\n]*\)(?:[+*?]|\{\d+(?:,\d*)?\})"
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _label_matches(node: Mapping[str, Any], label: str | None) -> bool:
    if not label:
        return True
    normalized = label.casefold()
    kind = str(node.get("node_kind") or "").casefold()
    aliases = {"function": "function", "method": "method", "class": "class", "file": "file", "route": "route", "module": "module", "package": "package", "service": "service"}
    return kind == aliases.get(normalized, normalized)


def _node_field(node: Mapping[str, Any], field: str) -> str:
    normalized = field.casefold()
    if normalized in {"kind", "node_kind"}:
        return str(node.get("node_kind") or "")
    return str(node.get(normalized) or "")


def _parse_query(query: str) -> dict[str, Any]:
    value = str(query or "").strip()
    if not value or len(value) > MAX_DSL_QUERY_CHARS:
        raise GraphDslError(f"query must be 1..{MAX_DSL_QUERY_CHARS} characters")
    if FORBIDDEN_TOKENS.search(value):
        raise GraphDslError("query contains a forbidden mutation or multi-statement token")
    match = _TWO_HOP_PATTERN_RE.fullmatch(value) or _PATTERN_RE.fullmatch(value)
    if not match:
        raise GraphDslError("supported form: MATCH (a:Label)-[:EDGE]->(b) WHERE a.name = 'x' RETURN a.name, b.path LIMIT n")
    data = match.groupdict()
    is_two_hop = bool(data.get("middle_alias"))
    operator = str(data.get("where_operator") or "").casefold()
    if operator and operator not in ALLOWED_OPERATORS:
        raise GraphDslError("unsupported WHERE operator")
    where_value = str(data.get("where_value") or "")
    if where_value:
        where_value = where_value[1:-1]
    if operator == "=~":
        if len(where_value) > MAX_DSL_REGEX_CHARS:
            raise GraphDslError(f"WHERE regex must be at most {MAX_DSL_REGEX_CHARS} characters")
        if _NESTED_REGEX_QUANTIFIER_RE.search(where_value):
            raise GraphDslError("WHERE regex contains nested quantifiers")
        try:
            re.compile(where_value, flags=re.IGNORECASE)
        except re.error as exc:
            raise GraphDslError("WHERE regex is invalid") from exc
    returns = []
    aggregate: dict[str, str] | None = None
    aliases = {data["left_alias"], data["right_alias"]}
    if is_two_hop:
        aliases.add(data["middle_alias"])
    for item in str(data["returns"]).split(","):
        aggregate_match = re.fullmatch(r"(?i:COUNT)\(([A-Za-z_][A-Za-z0-9_]*)\)(?:\s+(?i:AS)\s+([A-Za-z_][A-Za-z0-9_]*))?", item.strip())
        if aggregate_match:
            if aggregate is not None or returns:
                raise GraphDslError("aggregate COUNT cannot be mixed or repeated")
            aggregate_alias = aggregate_match.group(1)
            if aggregate_alias not in aliases:
                raise GraphDslError("COUNT references an unknown alias")
            aggregate = {"alias": aggregate_alias, "output": aggregate_match.group(2) or "count"}
            continue
        alias, field = [part.strip() for part in item.split(".", 1)]
        if alias not in aliases:
            raise GraphDslError("RETURN references an unknown alias")
        if field.casefold() not in ALLOWED_RETURN_FIELDS:
            raise GraphDslError(f"RETURN field is not allowlisted: {field}")
        returns.append((alias, field.casefold()))
    query_limit = int(data["query_limit"] or MAX_DSL_ROWS)
    if query_limit < 1 or query_limit > MAX_DSL_ROWS:
        raise GraphDslError(f"query LIMIT must be between 1 and {MAX_DSL_ROWS}")
    if data.get("where_alias") and data["where_alias"] not in aliases:
        raise GraphDslError("WHERE references an unknown alias")
    group_by = None
    if data.get("group_alias"):
        if not aggregate:
            raise GraphDslError("GROUP BY requires COUNT aggregate")
        group_alias = data["group_alias"]
        group_field = str(data.get("group_field") or "").casefold()
        if group_alias not in aliases:
            raise GraphDslError("GROUP BY references an unknown alias")
        if group_field not in {"kind", "node_kind", "name", "path", "language"}:
            raise GraphDslError("GROUP BY field is not allowlisted")
        group_by = {"alias": group_alias, "field": group_field}
    if aggregate and data.get("query_limit"):
        raise GraphDslError("aggregate COUNT cannot use LIMIT")
    return {
        "left_alias": data["left_alias"],
        "right_alias": data["right_alias"],
        "left_label": data.get("left_label"),
        "middle_alias": data.get("middle_alias"),
        "middle_label": data.get("middle_label"),
        "right_label": data.get("right_label"),
        "edge_kind": str(data.get("edge") or data.get("edge_one")).casefold(),
        "edge_kind_two": str(data.get("edge_two") or "").casefold(),
        "two_hop": is_two_hop,
        "where_alias": data.get("where_alias"),
        "where_field": str(data.get("where_field") or "").casefold(),
        "where_operator": operator,
        "where_value": where_value,
        "returns": returns,
        "aggregate": aggregate,
        "group_by": group_by,
        "query_limit": query_limit,
    }


def query_graph_dsl(
    database_path: str,
    *,
    project: str,
    root_id: str,
    query: str,
    limit: int = 32,
    offset: int = 0,
    time_budget_ms: float = 250.0,
) -> dict[str, Any]:
    if not 1 <= int(limit) <= MAX_DSL_ROWS:
        raise GraphDslError(f"limit must be between 1 and {MAX_DSL_ROWS}")
    if not 0 <= int(offset) <= MAX_DSL_OFFSET:
        raise GraphDslError(f"offset must be between 0 and {MAX_DSL_OFFSET}")
    if not 1.0 <= float(time_budget_ms) <= MAX_DSL_TIME_BUDGET_MS:
        raise GraphDslError(f"time_budget_ms must be between 1 and {MAX_DSL_TIME_BUDGET_MS}")
    parsed = _parse_query(query)
    effective_limit = min(int(limit), int(parsed["query_limit"]))
    if parsed["aggregate"] and int(offset) != 0:
        raise GraphDslError("aggregate COUNT requires offset=0")
    store = SQLiteCodeGraphStore(database_path)
    current = store.current_snapshot(project, root_id, include_material=False)
    snapshot_id = str(current.get("graph_snapshot_id")) if current else ""
    snapshot = store.snapshot(snapshot_id, include_material=True, read_only=True) if snapshot_id else None
    if snapshot is None:
        raise GraphDslError("graph snapshot unavailable; build WI-02 graph first")
    # The caller's budget applies to bounded traversal/compilation, not the
    # unavoidable SQLite snapshot read. Starting the clock before material
    # loading made healthy 1k-node graphs appear empty at the 250 ms default.
    started = time.perf_counter()
    nodes = list(snapshot.get("nodes") or [])
    edges = list(snapshot.get("edges") or [])
    nodes_by_id = {str(node.get("node_id")): node for node in nodes}
    sorted_edges = sorted(edges, key=lambda item: str(item.get("stable_key") or ""))
    candidates: list[tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]] = []
    adjacency: dict[str, list[Mapping[str, Any]]] = {}
    for edge in sorted_edges:
        adjacency.setdefault(str(edge.get("source_node_id")), []).append(edge)
    for edge in sorted_edges:
        if (time.perf_counter() - started) * 1_000 > float(time_budget_ms):
            break
        if str(edge.get("edge_kind") or "").casefold() != parsed["edge_kind"]:
            continue
        left = nodes_by_id.get(str(edge.get("source_node_id")))
        right = nodes_by_id.get(str(edge.get("target_node_id")))
        if not left or not right or not _label_matches(left, parsed["left_label"]):
            continue
        if not parsed["two_hop"] and not _label_matches(right, parsed["right_label"]):
            continue

        def where_matches(values: Mapping[str, Mapping[str, Any]]) -> bool:
            where_alias = parsed["where_alias"]
            if not where_alias:
                return True
            where_node = values.get(where_alias)
            if where_node is None:
                return False
            actual = _node_field(where_node, parsed["where_field"])
            expected = parsed["where_value"]
            operator = parsed["where_operator"]
            if operator == "=" and actual.casefold() != expected.casefold():
                return False
            if operator == "contains" and expected.casefold() not in actual.casefold():
                return False
            if operator == "=~":
                try:
                    if not re.search(expected, actual, flags=re.IGNORECASE):
                        return False
                except re.error as exc:
                    raise GraphDslError("WHERE regex is invalid") from exc
            return True
        if parsed["two_hop"]:
            for second in adjacency.get(str(right.get("node_id")), []):
                if str(second.get("edge_kind") or "").casefold() != parsed["edge_kind_two"]:
                    continue
                final = nodes_by_id.get(str(second.get("target_node_id")))
                if not final or not _label_matches(final, parsed["right_label"]):
                    continue
                if not where_matches({parsed["left_alias"]: left, parsed["middle_alias"]: right, parsed["right_alias"]: final}):
                    continue
                candidates.append(((left, right, final), (edge, second)))
                if len(candidates) >= MAX_DSL_PATHS:
                    break
            if len(candidates) >= MAX_DSL_PATHS:
                break
        else:
            if not where_matches({parsed["left_alias"]: left, parsed["right_alias"]: right}):
                continue
            candidates.append(((left, right), (edge,)))
        if len(candidates) >= MAX_DSL_PATHS:
            break
    total = len(candidates)
    rows: list[dict[str, Any]] = []
    selected = [] if parsed["aggregate"] else candidates[int(offset) : int(offset) + effective_limit]
    if parsed["aggregate"]:
        if parsed["group_by"]:
            grouped: dict[str, int] = {}
            group = parsed["group_by"]
            for path_nodes, _path_edges in candidates:
                values = {parsed["left_alias"]: path_nodes[0], parsed["right_alias"]: path_nodes[-1]}
                if parsed["two_hop"]:
                    values[parsed["middle_alias"]] = path_nodes[1]
                key = _node_field(values[group["alias"]], group["field"])
                grouped[key] = grouped.get(key, 0) + 1
            rows.extend(
                {group["field"]: key, parsed["aggregate"]["output"]: grouped[key]}
                for key in sorted(grouped)
            )
        else:
            rows.append({parsed["aggregate"]["output"]: total})
    else:
        for path_nodes, _path_edges in selected:
            values = {parsed["left_alias"]: path_nodes[0], parsed["right_alias"]: path_nodes[-1]}
            if parsed["two_hop"]:
                values[parsed["middle_alias"]] = path_nodes[1]
            rows.append({f"{alias}.{field}": _node_field(values[alias], field) for alias, field in parsed["returns"]})
    unique_nodes: dict[str, Mapping[str, Any]] = {}
    unique_edges: dict[str, Mapping[str, Any]] = {}
    for path_nodes, path_edges in selected:
        for node in path_nodes:
            unique_nodes[str(node.get("node_id"))] = node
        for edge in path_edges:
            unique_edges[str(edge.get("stable_key"))] = edge
    response: dict[str, Any] = {
        "schema_version": CODE_GRAPH_DSL_SCHEMA_VERSION,
        "graph_schema_version": CODE_GRAPH_SCHEMA_VERSION,
        "operation": "graph_query",
        "query": query,
        "project": project,
        "root_id": root_id,
        "snapshot_id": snapshot.get("graph_snapshot_id"),
        "graph_digest": snapshot.get("graph_digest"),
        "rows": rows,
        "nodes": [_node_view(node) for node in unique_nodes.values()],
        "edges": [_edge_view(edge, nodes_by_id) for edge in unique_edges.values()],
        "pagination": {"offset": int(offset), "limit": effective_limit, "next_offset": None if parsed["aggregate"] else (int(offset) + len(selected) if int(offset) + len(selected) < total else None), "total_rows": len(rows) if parsed["aggregate"] else total},
        "query_plan": {"compiler": "bhm.allowlisted-graph-dsl.v4", "pattern": {"edge_kind": parsed["edge_kind"], "edge_kind_two": parsed["edge_kind_two"], "left_label": parsed["left_label"], "middle_label": parsed["middle_label"], "right_label": parsed["right_label"], "two_hop": parsed["two_hop"]}, "aggregate": parsed["aggregate"], "group_by": parsed["group_by"], "allowlisted": True, "read_only": True, "arbitrary_sql": False, "bounded": True, "candidate_rows": total, "candidate_node_count": len(unique_nodes), "candidate_edge_count": len(unique_edges), "candidate_cap": MAX_DSL_PATHS},
        "bounds": {"limit": effective_limit, "offset": int(offset), "time_budget_ms": float(time_budget_ms), "elapsed_ms": round((time.perf_counter() - started) * 1_000, 3), "budget_exceeded": (time.perf_counter() - started) * 1_000 > float(time_budget_ms), "truncated": bool(not parsed["aggregate"] and int(offset) + len(selected) < total)},
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "raw_source_returned": False, "arbitrary_sql": False, "autonomous_apply": False},
        "provenance": {"source": "sqlite-authoritative", "authority": "read-only graph query", "raw_source_returned": False},
    }
    response["quality_receipt"] = build_graph_query_quality_receipt(response)
    # Wall-clock timing is diagnostic only and must not perturb the reproducible
    # contract digest used by catalog/drift checks.
    digest_payload = {key: value for key, value in response.items() if key != "response_digest"}
    digest_payload["bounds"] = dict(response["bounds"], elapsed_ms=0.0)
    response["response_digest"] = _digest(digest_payload)
    return response


__all__ = ["CODE_GRAPH_DSL_SCHEMA_VERSION", "GraphDslError", "query_graph_dsl"]
