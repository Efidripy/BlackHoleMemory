"""Bounded read-only query and explain capability for the WI-02 graph."""

from __future__ import annotations

import hashlib
import json
import re
import time
from fnmatch import fnmatchcase
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Mapping, Sequence

from .code_graph import CODE_GRAPH_SCHEMA_VERSION
from .code_graph import SQLiteCodeGraphStore
from .graph_query_quality_receipt import build_graph_query_quality_receipt
from .graph_edge_taxonomy_receipt import build_graph_edge_taxonomy_receipt
from .graph_path_explain_quality_receipt import build_graph_path_explain_quality_receipt
from .security_boundaries import SecurityBoundaryError
from .security_boundaries import compile_bounded_regex


CODE_GRAPH_QUERY_SCHEMA_VERSION = "bhm.code-graph.query.v1"
CODE_GRAPH_EXPLAIN_SCHEMA_VERSION = "bhm.code-graph.explain.v1"
CODE_GRAPH_EXPLAIN_RECEIPT_SCHEMA_VERSION = "bhm.code-graph.explain-receipt.v1"
DEFAULT_QUERY_LIMIT = 32
MAX_QUERY_LIMIT = 128
DEFAULT_QUERY_OFFSET = 0
MAX_QUERY_OFFSET = 10_000
MAX_QUERY_DEPTH = 8
DEFAULT_QUERY_TOKEN_BUDGET = 4_096
MAX_QUERY_TOKEN_BUDGET = 16_384
DEFAULT_QUERY_TIME_BUDGET_MS = 250.0
MAX_QUERY_TIME_BUDGET_MS = 5_000.0
ALLOWED_OPERATIONS = frozenset({"symbol", "resolve", "callers", "callees", "imports", "importers", "routes", "tests", "impact", "neighborhood", "degree"})
KNOWN_EDGE_KINDS = frozenset({
    "calls", "async_calls", "imports", "tests", "inherits", "route_handles",
    "http_calls", "emits", "listens_on", "data_flows", "similar_to",
    "depends_on", "exposes", "contains",
})


class CodeGraphQueryError(ValueError):
    """Raised when a bounded query is invalid or unavailable."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clip(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _validate_query_text(query: str, *, operation: str = "") -> str:
    value = str(query or "").strip()
    if len(value) > 480:
        raise CodeGraphQueryError("query exceeds 480 characters")
    normalized = value.replace("\\", "/")
    if (normalized.startswith("/") and operation != "routes") or re.match(r"^[A-Za-z]:/", normalized) or "/../" in f"/{normalized}/" or normalized.startswith("../"):
        raise CodeGraphQueryError("query path escapes registered repository root")
    return value


def _validate_bounds(operation: str, depth: int, limit: int, offset: int, max_tokens: int, time_budget_ms: float) -> None:
    if operation not in ALLOWED_OPERATIONS:
        raise CodeGraphQueryError(f"unsupported graph operation: {operation}")
    if not 0 <= int(depth) <= MAX_QUERY_DEPTH:
        raise CodeGraphQueryError(f"depth must be between 0 and {MAX_QUERY_DEPTH}")
    if not 1 <= int(limit) <= MAX_QUERY_LIMIT:
        raise CodeGraphQueryError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")
    if not 0 <= int(offset) <= MAX_QUERY_OFFSET:
        raise CodeGraphQueryError(f"offset must be between 0 and {MAX_QUERY_OFFSET}")
    if not 128 <= int(max_tokens) <= MAX_QUERY_TOKEN_BUDGET:
        raise CodeGraphQueryError(f"max_tokens must be between 128 and {MAX_QUERY_TOKEN_BUDGET}")
    if not 1.0 <= float(time_budget_ms) <= MAX_QUERY_TIME_BUDGET_MS:
        raise CodeGraphQueryError(f"time_budget_ms must be between 1 and {MAX_QUERY_TIME_BUDGET_MS}")


def _normalize_edge_filter(edge_kinds: Sequence[str] | None) -> set[str] | None:
    if edge_kinds is None:
        return None
    values = {str(value or "").strip().casefold() for value in edge_kinds if str(value or "").strip()}
    if len(values) > 16:
        raise CodeGraphQueryError("edge_kinds accepts at most 16 values")
    unknown = sorted(values - KNOWN_EDGE_KINDS)
    if unknown:
        raise CodeGraphQueryError(f"unsupported edge kind: {unknown[0]}")
    return values


def _node_view(node: Mapping[str, Any]) -> dict[str, Any]:
    provenance = node.get("provenance") or {}
    return {
        "node_id": node.get("node_id"),
        "stable_key": node.get("stable_key"),
        "node_kind": node.get("node_kind"),
        "path": node.get("path") or "",
        "name": node.get("name") or "",
        "qualified_name": node.get("qualified_name") or "",
        "language": node.get("language") or "",
        "span": {"start_line": node.get("start_line"), "end_line": node.get("end_line")},
        "signature": node.get("signature") or "",
        "content_sha256": node.get("content_sha256") or "",
        "parser_version": node.get("parser_version") or "",
        "source_ref": provenance.get("source_ref") or "",
        "attributes": node.get("attributes") or {},
    }


def _edge_view(edge: Mapping[str, Any], nodes_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    source = nodes_by_id.get(str(edge.get("source_node_id"))) or {}
    target = nodes_by_id.get(str(edge.get("target_node_id"))) or {}
    return {
        "edge_id": edge.get("edge_id"),
        "stable_key": edge.get("stable_key"),
        "edge_kind": edge.get("edge_kind"),
        "source_node_id": edge.get("source_node_id"),
        "target_node_id": edge.get("target_node_id"),
        "source_stable_key": source.get("stable_key") or "",
        "target_stable_key": target.get("stable_key") or "",
        "confidence": float(edge.get("confidence") or 0.0),
        "unresolved": bool(edge.get("unresolved")),
        "extractor_version": edge.get("extractor_version") or "",
        "evidence": edge.get("evidence") or {},
        "attributes": edge.get("attributes") or {},
    }


def _validate_structural_filters(
    *,
    name_pattern: str | None,
    path_pattern: str | None,
    label: str | None,
    min_degree: int | None,
    max_degree: int | None,
) -> tuple[re.Pattern[str] | None, str | None, str | None, int | None, int | None]:
    compiled = None
    if name_pattern:
        try:
            compiled = compile_bounded_regex(name_pattern, field="name_pattern")
        except SecurityBoundaryError as exc:
            raise CodeGraphQueryError(str(exc)) from exc
    normalized_label = str(label or "").strip().casefold() or None
    if normalized_label and (len(normalized_label) > 40 or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", normalized_label)):
        raise CodeGraphQueryError("label must be a simple node-kind identifier")
    normalized_path = str(path_pattern or "").strip() or None
    if normalized_path and len(normalized_path) > 240:
        raise CodeGraphQueryError("path_pattern exceeds 240 characters")
    lo = int(min_degree) if min_degree is not None else None
    hi = int(max_degree) if max_degree is not None else None
    if lo is not None and lo < 0:
        raise CodeGraphQueryError("min_degree must be non-negative")
    if hi is not None and hi < 0:
        raise CodeGraphQueryError("max_degree must be non-negative")
    if lo is not None and hi is not None and lo > hi:
        raise CodeGraphQueryError("min_degree cannot exceed max_degree")
    return compiled, normalized_path, normalized_label, lo, hi


def _match_nodes(
    nodes: list[Mapping[str, Any]],
    operation: str,
    query: str,
    *,
    name_pattern: re.Pattern[str] | None = None,
    path_pattern: str | None = None,
    label: str | None = None,
    degree_by_node: Mapping[str, int] | None = None,
    min_degree: int | None = None,
    max_degree: int | None = None,
) -> list[Mapping[str, Any]]:
    needle = query.casefold()
    candidates = [node for node in nodes if operation != "routes" or node.get("node_kind") == "route"]
    if label:
        candidates = [node for node in candidates if str(node.get("node_kind") or "").casefold() == label]
    if path_pattern:
        candidates = [node for node in candidates if fnmatchcase(str(node.get("path") or ""), path_pattern)]
    if name_pattern:
        candidates = [node for node in candidates if name_pattern.search(str(node.get("name") or ""))]
    if degree_by_node is not None and (min_degree is not None or max_degree is not None):
        candidates = [
            node for node in candidates
            if (min_degree is None or int(degree_by_node.get(str(node.get("node_id") or ""), 0)) >= min_degree)
            and (max_degree is None or int(degree_by_node.get(str(node.get("node_id") or ""), 0)) <= max_degree)
        ]
    if not needle:
        if operation == "routes":
            return sorted(candidates, key=lambda item: str(item.get("stable_key") or ""))
        return sorted([node for node in candidates if node.get("node_kind") in {"repository", "file", "module", "package", "class", "interface", "enum", "struct", "record", "trait", "object", "type", "message", "service", "namespace", "function", "method", "test", "config_key", "section", "markup_tag", "style_selector", "heading"}], key=lambda item: str(item.get("stable_key") or ""))
    matched = [
        node
        for node in candidates
        if any(needle in str(node.get(field) or "").casefold() for field in ("stable_key", "path", "name", "qualified_name", "signature"))
    ]
    # Resolution prefers exact qualified/name matches, then path/module matches.
    if operation == "resolve":
        return sorted(matched, key=lambda item: (
            0 if str(item.get("qualified_name") or "").casefold() == needle else 1,
            0 if str(item.get("name") or "").casefold() == needle else 1,
            0 if str(item.get("path") or "").casefold() == needle else 1,
            str(item.get("path") or ""),
            int(item.get("start_line") or 0),
            str(item.get("stable_key") or ""),
        ))
    return sorted(matched, key=lambda item: (str(item.get("path") or ""), int(item.get("start_line") or 0), str(item.get("stable_key") or "")))


def _alias_tokens(value: Any) -> set[str]:
    """Extract bounded identifier-like import aliases from graph metadata."""

    return {token.casefold() for token in re.findall(r"[A-Za-z_$][A-Za-z0-9_$.-]{0,120}", str(value or ""))}


def _resolve_import_aliases(
    nodes: list[Mapping[str, Any]],
    edges: list[Mapping[str, Any]],
    query: str,
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    """Resolve import aliases to graph metadata without reading source text."""

    needle = query.casefold()
    nodes_by_id = {str(node.get("node_id")): node for node in nodes}
    module_by_file: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for edge in edges:
        if str(edge.get("edge_kind") or "") != "contains":
            continue
        source = nodes_by_id.get(str(edge.get("source_node_id") or ""))
        target_id = str(edge.get("target_node_id") or "")
        if source and source.get("node_kind") in {"module", "package"}:
            module_by_file[target_id].append(source)
    candidates: dict[str, Mapping[str, Any]] = {}
    matches: list[dict[str, Any]] = []
    for edge in sorted(edges, key=lambda item: str(item.get("stable_key") or "")):
        if str(edge.get("edge_kind") or "") != "imports":
            continue
        attributes = dict(edge.get("attributes") or {})
        module = str(attributes.get("module") or "")
        alias = str(attributes.get("alias") or "")
        module_tokens = _alias_tokens(module)
        alias_tokens = _alias_tokens(alias)
        if needle not in module_tokens and needle not in alias_tokens and needle not in module.casefold():
            continue
        target_id = str(edge.get("target_node_id") or "")
        target = nodes_by_id.get(target_id)
        if target is None:
            continue
        candidates[target_id] = target
        for module_node in module_by_file.get(target_id, []):
            candidates[str(module_node.get("node_id") or "")] = module_node
        matches.append(
            {
                "source_stable_key": nodes_by_id.get(str(edge.get("source_node_id") or ""), {}).get("stable_key") or "",
                "target_node_id": target_id,
                "target_stable_key": target.get("stable_key") or "",
                "module": module[:300],
                "alias": alias[:300],
                "confidence": float(edge.get("confidence") or 0.0),
                "match_kind": "alias" if needle in alias_tokens else "module",
            }
        )
    return list(candidates.values()), matches[:32]


def _directions(operation: str) -> tuple[set[str], bool]:
    if operation == "callers":
        return {"calls", "async_calls"}, True
    if operation == "callees":
        return {"calls", "async_calls"}, False
    if operation == "imports":
        return {"imports"}, False
    if operation == "importers":
        return {"imports"}, True
    if operation == "tests":
        return {"tests"}, True
    if operation == "impact":
        return {"calls", "async_calls", "imports", "tests", "inherits", "route_handles", "http_calls", "emits", "listens_on", "data_flows", "similar_to", "depends_on", "exposes"}, True
    if operation == "neighborhood":
        return {"contains", "imports", "calls", "async_calls", "inherits", "tests", "route_handles", "http_calls", "emits", "listens_on", "data_flows", "similar_to", "depends_on", "exposes"}, False
    if operation == "routes":
        return {"route_handles"}, False
    if operation == "degree":
        # Degree is a bounded metadata metric, not a traversal.  Returning
        # the known edge kinds here keeps an optional edge_kinds filter
        # explicit while avoiding an unbounded/all-SQL query surface.
        return set(KNOWN_EDGE_KINDS), False
    return set(), False


def _bounded_material(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    max_tokens: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    selected_nodes: list[dict[str, Any]] = []
    selected_edges: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for node in nodes:
        cost = max(1, len(_canonical_json(node)) // 4)
        if used + cost > max_tokens:
            truncated = True
            break
        selected_nodes.append(node)
        used += cost
    allowed = {str(node.get("node_id")) for node in selected_nodes}
    for edge in edges:
        if str(edge.get("source_node_id")) not in allowed or str(edge.get("target_node_id")) not in allowed:
            continue
        cost = max(1, len(_canonical_json(edge)) // 4)
        if used + cost > max_tokens:
            truncated = True
            break
        selected_edges.append(edge)
        used += cost
    return selected_nodes, selected_edges, truncated


def query_code_graph(
    database_path: str | Path,
    *,
    project: str,
    root_id: str,
    operation: str,
    query: str = "",
    depth: int = 2,
    limit: int = DEFAULT_QUERY_LIMIT,
    offset: int = DEFAULT_QUERY_OFFSET,
    max_tokens: int = DEFAULT_QUERY_TOKEN_BUDGET,
    time_budget_ms: float = DEFAULT_QUERY_TIME_BUDGET_MS,
    snapshot_id: str | None = None,
    explain: bool = False,
    edge_kinds: Sequence[str] | None = None,
    name_pattern: str | None = None,
    path_pattern: str | None = None,
    label: str | None = None,
    min_degree: int | None = None,
    max_degree: int | None = None,
) -> dict[str, Any]:
    operation = str(operation or "").strip().casefold()
    query = _validate_query_text(query, operation=operation)
    _validate_bounds(operation, depth, limit, offset, max_tokens, time_budget_ms)
    requested_edge_kinds = _normalize_edge_filter(edge_kinds)
    compiled_name_pattern, normalized_path_pattern, normalized_label, normalized_min_degree, normalized_max_degree = _validate_structural_filters(
        name_pattern=name_pattern,
        path_pattern=path_pattern,
        label=label,
        min_degree=min_degree,
        max_degree=max_degree,
    )
    started = time.perf_counter()
    store = SQLiteCodeGraphStore(database_path)
    current = store.current_snapshot(project, root_id, include_material=False)
    selected_snapshot_id = snapshot_id or (str(current.get("graph_snapshot_id")) if current else None)
    snapshot = store.snapshot(selected_snapshot_id, include_material=True, read_only=True) if selected_snapshot_id else None
    if snapshot is None:
        raise CodeGraphQueryError("graph snapshot unavailable; build WI-02 graph first")
    stale = bool(current and current.get("graph_snapshot_id") != snapshot.get("graph_snapshot_id"))
    nodes = list(snapshot.get("nodes") or [])
    edges = list(snapshot.get("edges") or [])
    nodes_by_id = {str(node.get("node_id")): node for node in nodes}
    edges_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    edges_by_target: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for edge in edges:
        edges_by_source[str(edge.get("source_node_id"))].append(edge)
        edges_by_target[str(edge.get("target_node_id"))].append(edge)
    degree_by_node: dict[str, int] = defaultdict(int)
    for edge in edges:
        degree_by_node[str(edge.get("source_node_id") or "")] += 1
        degree_by_node[str(edge.get("target_node_id") or "")] += 1
    alias_matches: list[dict[str, Any]] = []
    matched_seeds = _match_nodes(
        nodes,
        operation,
        query,
        name_pattern=compiled_name_pattern,
        path_pattern=normalized_path_pattern,
        label=normalized_label,
        degree_by_node=degree_by_node,
        min_degree=normalized_min_degree,
        max_degree=normalized_max_degree,
    )
    if operation == "resolve" and query:
        alias_candidates, alias_matches = _resolve_import_aliases(nodes, edges, query)
        existing_ids = {str(node.get("node_id") or "") for node in matched_seeds}
        matched_seeds.extend(node for node in alias_candidates if str(node.get("node_id") or "") not in existing_ids)
        matched_seeds.sort(
            key=lambda item: (
                0 if str(item.get("qualified_name") or "").casefold() == query.casefold() else 1,
                0 if str(item.get("name") or "").casefold() == query.casefold() else 1,
                0 if any(str(item.get("node_id") or "") == str(match.get("target_node_id") or "") for match in alias_matches) else 1,
                str(item.get("path") or ""),
                int(item.get("start_line") or 0),
                str(item.get("stable_key") or ""),
            )
        )
    total_seed_count = len(matched_seeds)
    seeds = matched_seeds[int(offset) : int(offset) + int(limit)]
    seed_ids = {str(node.get("node_id")) for node in seeds}
    result_ids = set(seed_ids)
    traversed: list[Mapping[str, Any]] = []
    parent: dict[str, tuple[str, Mapping[str, Any]]] = {}
    operation_edge_kinds, reverse = _directions(operation)
    active_edge_kinds = operation_edge_kinds if requested_edge_kinds is None else operation_edge_kinds & requested_edge_kinds
    if operation not in {"symbol", "resolve", "degree"}:
        queue: deque[tuple[str, int]] = deque((node_id, 0) for node_id in sorted(seed_ids))
        while queue and len(result_ids) < int(limit):
            if (time.perf_counter() - started) * 1_000 > float(time_budget_ms):
                break
            current_id, current_depth = queue.popleft()
            if current_depth >= int(depth):
                continue
            candidate_edges = edges_by_target.get(current_id, []) if reverse else edges_by_source.get(current_id, [])
            if operation == "neighborhood":
                candidate_edges = edges_by_source.get(current_id, []) + edges_by_target.get(current_id, [])
            for edge in sorted(candidate_edges, key=lambda item: str(item.get("stable_key") or "")):
                if requested_edge_kinds is not None and str(edge.get("edge_kind")) not in active_edge_kinds:
                    continue
                next_id = str(edge.get("source_node_id")) if reverse and operation != "neighborhood" else str(edge.get("target_node_id"))
                if operation == "neighborhood":
                    next_id = str(edge.get("target_node_id")) if str(edge.get("source_node_id")) == current_id else str(edge.get("source_node_id"))
                traversed.append(edge)
                if next_id not in result_ids:
                    result_ids.add(next_id)
                    parent[next_id] = (current_id, edge)
                    queue.append((next_id, current_depth + 1))
                    if len(result_ids) >= int(limit):
                        break
    else:
        traversed = []
    if operation == "routes" and query:
        for edge in edges:
            if requested_edge_kinds is not None and str(edge.get("edge_kind")) not in active_edge_kinds:
                continue
            if str(edge.get("edge_kind")) == "route_handles" and (str(edge.get("source_node_id")) in seed_ids or str(edge.get("target_node_id")) in seed_ids):
                traversed.append(edge)
                result_ids.update({str(edge.get("source_node_id")), str(edge.get("target_node_id"))})
    result_nodes = sorted([_node_view(nodes_by_id[node_id]) for node_id in result_ids if node_id in nodes_by_id], key=lambda item: (str(item.get("path") or ""), int((item.get("span") or {}).get("start_line") or 0), str(item.get("stable_key") or "")))[: int(limit)]
    if operation == "degree":
        # Emit only deterministic graph metadata; never source, SQL or
        # payloads.  The edge-kind filter applies to the metric itself.
        metric_edge_kinds = active_edge_kinds or set(KNOWN_EDGE_KINDS)
        degree_metrics: dict[str, dict[str, Any]] = defaultdict(lambda: {"in_degree": 0, "out_degree": 0, "edge_kind_counts": defaultdict(int)})
        for edge in edges:
            edge_kind = str(edge.get("edge_kind") or "").casefold()
            if edge_kind not in metric_edge_kinds:
                continue
            source_id = str(edge.get("source_node_id") or "")
            target_id = str(edge.get("target_node_id") or "")
            if source_id in result_ids:
                degree_metrics[source_id]["out_degree"] += 1
                degree_metrics[source_id]["edge_kind_counts"][edge_kind] += 1
            if target_id in result_ids:
                degree_metrics[target_id]["in_degree"] += 1
                degree_metrics[target_id]["edge_kind_counts"][edge_kind] += 1
        for node in result_nodes:
            metrics = degree_metrics[str(node.get("node_id") or "")]
            node["graph_metrics"] = {
                "schema_version": "bhm.code-graph.degree.v1",
                "degree": int(metrics["in_degree"] + metrics["out_degree"]),
                "in_degree": int(metrics["in_degree"]),
                "out_degree": int(metrics["out_degree"]),
                "edge_kind_counts": {key: int(value) for key, value in sorted(metrics["edge_kind_counts"].items())},
                "edge_kinds": sorted(metric_edge_kinds),
            }
    result_node_ids = {str(node.get("node_id")) for node in result_nodes}
    unique_edges: dict[str, dict[str, Any]] = {}
    for edge in traversed:
        view = _edge_view(edge, nodes_by_id)
        if str(view.get("source_node_id")) in result_node_ids and str(view.get("target_node_id")) in result_node_ids:
            unique_edges[str(view.get("stable_key"))] = view
    if operation in {"symbol", "resolve"}:
        for edge in edges:
            if requested_edge_kinds is not None and str(edge.get("edge_kind")) not in active_edge_kinds:
                continue
            if str(edge.get("source_node_id")) in result_node_ids and str(edge.get("target_node_id")) in result_node_ids:
                unique_edges[str(edge.get("stable_key"))] = _edge_view(edge, nodes_by_id)
    result_edges = sorted(unique_edges.values(), key=lambda item: str(item.get("stable_key") or ""))
    bounded_nodes, bounded_edges, truncated = _bounded_material(result_nodes, result_edges, max_tokens=int(max_tokens))
    explanations: list[dict[str, Any]] = []
    if explain:
        for node in bounded_nodes:
            node_id = str(node.get("node_id"))
            if node_id in seed_ids:
                explanations.append({"node_id": node_id, "stable_key": node.get("stable_key"), "reason": "seed_match", "path": [], "source_refs": [node.get("source_ref")] if node.get("source_ref") else [], "path_receipt": {"schema_version": CODE_GRAPH_EXPLAIN_RECEIPT_SCHEMA_VERSION, "hops": 0, "cost": 0.0, "edge_provenance": []}})
                continue
            path_edges: list[dict[str, Any]] = []
            cursor = node_id
            seen: set[str] = set()
            while cursor in parent and cursor not in seen and len(path_edges) < int(depth):
                seen.add(cursor)
                previous, edge = parent[cursor]
                path_edges.append(_edge_view(edge, nodes_by_id))
                cursor = previous
            path_edges.reverse()
            source_refs = sorted({ref for edge in path_edges for ref in (edge.get("evidence") or {}).get("source_refs", []) if ref})
            if node.get("source_ref"):
                source_refs.append(str(node["source_ref"]))
            edge_provenance = [
                {
                    "edge_stable_key": edge.get("stable_key") or "",
                    "edge_kind": edge.get("edge_kind") or "",
                    "confidence": float(edge.get("confidence") or 0.0),
                    "unresolved": bool(edge.get("unresolved")),
                    "extractor_version": edge.get("extractor_version") or "",
                    "provenance_digest": _sha256(_canonical_json({"evidence": edge.get("evidence") or {}, "attributes": edge.get("attributes") or {}})),
                }
                for edge in path_edges
            ]
            path_cost = round(sum(1.0 + (1.0 - item["confidence"]) + (1.0 if item["unresolved"] else 0.0) for item in edge_provenance), 6)
            explanations.append({"node_id": node_id, "stable_key": node.get("stable_key"), "reason": "traversal", "path": [edge.get("stable_key") for edge in path_edges], "source_refs": sorted(set(source_refs))[:32], "path_receipt": {"schema_version": CODE_GRAPH_EXPLAIN_RECEIPT_SCHEMA_VERSION, "hops": len(edge_provenance), "cost": path_cost, "edge_provenance": edge_provenance}})
    response: dict[str, Any] = {
        "schema_version": CODE_GRAPH_EXPLAIN_SCHEMA_VERSION if explain else CODE_GRAPH_QUERY_SCHEMA_VERSION,
        "graph_schema_version": CODE_GRAPH_SCHEMA_VERSION,
        "operation": operation,
        "query": query,
        "project": project,
        "root_id": root_id,
        "snapshot_id": snapshot.get("graph_snapshot_id"),
        "repository_snapshot_id": snapshot.get("repository_snapshot_id"),
        "graph_digest": snapshot.get("graph_digest"),
        "graph_hash": snapshot.get("graph_digest"),
        "stale": stale,
        "nodes": bounded_nodes,
        "edges": bounded_edges,
        "explanations": explanations if explain else [],
        "query_plan": {
            "compiler": "bhm.allowlisted-graph-query.v1",
            "operation": operation,
            "read_only": True,
            "allowlisted": operation in ALLOWED_OPERATIONS,
            "seed_count": len(seeds),
            "seed_total_count": total_seed_count,
            "candidate_node_count": len(result_ids),
            "candidate_edge_count": len(result_edges),
            "edge_kinds": sorted(active_edge_kinds),
            "requested_edge_kinds": sorted(requested_edge_kinds) if requested_edge_kinds is not None else [],
            "filters": {
                "name_pattern": name_pattern or "",
                "path_pattern": normalized_path_pattern or "",
                "label": normalized_label or "",
                "min_degree": normalized_min_degree,
                "max_degree": normalized_max_degree,
            },
            "metric": "degree" if operation == "degree" else "",
            "bounded": True,
            "arbitrary_sql": False,
        },
        "bounds": {"depth": int(depth), "limit": int(limit), "offset": int(offset), "max_tokens": int(max_tokens), "time_budget_ms": float(time_budget_ms), "elapsed_ms": round((time.perf_counter() - started) * 1_000, 3), "budget_exceeded": (time.perf_counter() - started) * 1_000 > float(time_budget_ms), "truncated": bool(truncated or len(result_ids) >= int(limit) or (time.perf_counter() - started) * 1_000 > float(time_budget_ms))},
        "pagination": {"offset": int(offset), "limit": int(limit), "next_offset": int(offset) + len(seeds) if int(offset) + len(seeds) < total_seed_count else None, "total_seed_count": total_seed_count},
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_retrieval": False, "model_started": False, "raw_source_returned": False, "arbitrary_sql": False},
    }
    if explain:
        response["explain_receipt"] = {
            "schema_version": CODE_GRAPH_EXPLAIN_RECEIPT_SCHEMA_VERSION,
            "algorithm": "bounded-edge-provenance-cost.v1",
            "path_count": len(explanations),
            "max_hops": int(depth),
            "metadata_only": True,
            "raw_source_returned": False,
        }
        response["path_explain_quality_receipt"] = build_graph_path_explain_quality_receipt(response)
    if operation == "resolve":
        exact_qualified = [node for node in bounded_nodes if str(node.get("qualified_name") or "").casefold() == query.casefold()]
        exact_name = [node for node in bounded_nodes if str(node.get("name") or "").casefold() == query.casefold()]
        response["resolution"] = {
            "strategy": "exact-qualified-then-name-then-import-alias-then-path",
            "candidate_count": len(seeds),
            "exact_qualified_count": len(exact_qualified),
            "exact_name_count": len(exact_name),
            "ambiguous": len(seeds) > 1 and not exact_qualified,
            "resolved": bool(exact_qualified or exact_name or alias_matches),
            "alias_matches": alias_matches,
            "alias_match_count": len(alias_matches),
            "metadata_only": True,
        }
    response["quality_receipt"] = build_graph_query_quality_receipt(response)
    response["edge_taxonomy_receipt"] = build_graph_edge_taxonomy_receipt(response)
    digest_payload = {key: value for key, value in response.items() if key != "response_digest"}
    digest_bounds = dict(digest_payload.get("bounds") or {})
    digest_bounds["elapsed_ms"] = 0.0
    digest_payload["bounds"] = digest_bounds
    response["response_digest"] = _sha256(_canonical_json(digest_payload))
    return response


def explain_code_graph(*args: Any, **kwargs: Any) -> dict[str, Any]:
    kwargs["explain"] = True
    return query_code_graph(*args, **kwargs)


__all__ = [
    "ALLOWED_OPERATIONS",
    "CODE_GRAPH_EXPLAIN_SCHEMA_VERSION",
    "CODE_GRAPH_EXPLAIN_RECEIPT_SCHEMA_VERSION",
    "CODE_GRAPH_QUERY_SCHEMA_VERSION",
    "CodeGraphQueryError",
    "explain_code_graph",
    "query_code_graph",
]
