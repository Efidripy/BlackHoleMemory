"""Bounded change-impact preview construction."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Mapping, Sequence

from .change_impact_base import (
    CHANGE_IMPACT_SCHEMA_VERSION,
    IMPACT_EDGE_KINDS,
    IMPACT_PREVIEW_RESPONSE_BOUNDS_SCHEMA_VERSION,
    IMPACT_PREVIEW_RESPONSE_MAX_SERIALIZED_BYTES,
    MAX_CHANGED_PATHS,
    MAX_IMPACT_NODES,
    MAX_TEST_PATHS,
    MIN_CONFIDENCE,
    ChangeImpactError,
    _architecture_map,
    _bounded_text,
    _compact_impact_metadata,
    _node_key,
    _path,
    _patterns,
    _serialized_json_bytes,
    _sha256,
)


def _impact_preview_fallback(response: Mapping[str, Any]) -> dict[str, Any]:
    """Keep review-critical metadata when detailed preview material is too large."""

    binding = response.get("impact_binding") if isinstance(response.get("impact_binding"), Mapping) else {}
    risk = response.get("risk_receipt") if isinstance(response.get("risk_receipt"), Mapping) else {}
    gates = response.get("gates") if isinstance(response.get("gates"), Mapping) else {}
    execution = response.get("execution") if isinstance(response.get("execution"), Mapping) else {}
    provenance = response.get("provenance") if isinstance(response.get("provenance"), Mapping) else {}
    return {
        "schema_version": _bounded_text(response.get("schema_version"), 128),
        "preview_digest": _bounded_text(response.get("preview_digest"), 128),
        "operation": _bounded_text(response.get("operation"), 64),
        "contract_digest": _bounded_text(response.get("contract_digest"), 128),
        "project": _bounded_text(response.get("project"), 160),
        "graph_snapshot_id": _bounded_text(response.get("graph_snapshot_id"), 192),
        "graph_digest": _bounded_text(response.get("graph_digest"), 192),
        "changed_paths": [_bounded_text(value, 384) for value in list(response.get("changed_paths") or [])[:MAX_CHANGED_PATHS]],
        "selectedTests": [_bounded_text(value, 384) for value in list(response.get("selectedTests") or [])[:MAX_TEST_PATHS]],
        "whatWouldHelp": [_bounded_text(value, 384) for value in list(response.get("whatWouldHelp") or [])[:16]],
        "ready": bool(response.get("ready")),
        "stale": bool(response.get("stale")),
        "graph_stale": bool(response.get("graph_stale")),
        "low_confidence": bool(response.get("low_confidence")),
        "gates": {
            "coverage_complete": bool(gates.get("coverage_complete")),
            "human_review_required": bool(gates.get("human_review_required")),
            "graph_digest_bound": bool(gates.get("graph_digest_bound")),
        },
        "impact_binding": {
            "schema_version": _bounded_text(binding.get("schema_version"), 128),
            "status": _bounded_text(binding.get("status"), 64),
            "ok": bool(binding.get("ok")),
            "coverage": _compact_impact_metadata(binding.get("coverage") or {}, collection_limit=16, mapping_limit=16, text_limit=160, depth_limit=3),
            "gaps": [_bounded_text(value, 160) for value in list(binding.get("gaps") or [])[:32]],
            "failures": [_bounded_text(value, 160) for value in list(binding.get("failures") or [])[:32]],
            "execution": _compact_impact_metadata(binding.get("execution") or {}, collection_limit=16, mapping_limit=16, text_limit=160, depth_limit=3),
        },
        "risk_receipt": {
            "schema_version": _bounded_text(risk.get("schema_version"), 128),
            "status": _bounded_text(risk.get("status"), 64),
            "risk_bucket": _bounded_text(risk.get("risk_bucket"), 64),
            "disposition": _bounded_text(risk.get("disposition"), 64),
            "coverage": _compact_impact_metadata(risk.get("coverage") or {}, collection_limit=16, mapping_limit=16, text_limit=160, depth_limit=3),
            "signals": _compact_impact_metadata(risk.get("signals") or {}, collection_limit=16, mapping_limit=16, text_limit=160, depth_limit=3),
            "gaps": [_bounded_text(value, 160) for value in list(risk.get("gaps") or [])[:32]],
            "execution": _compact_impact_metadata(risk.get("execution") or {}, collection_limit=16, mapping_limit=16, text_limit=160, depth_limit=3),
        },
        "provenance": {
            "source": _bounded_text(provenance.get("source"), 160),
            "graph_snapshot_id": _bounded_text(provenance.get("graph_snapshot_id"), 192),
            "raw_source_returned": False,
        },
        "execution": _compact_impact_metadata(execution, collection_limit=16, mapping_limit=16, text_limit=160, depth_limit=3),
    }


def _with_impact_preview_response_bounds(
    response: Mapping[str, Any],
    *,
    max_serialized_bytes: int,
    original_serialized_bytes: int,
    truncated: bool,
    strategy: str,
) -> dict[str, Any]:
    bounded = dict(response)
    bounded["response_bounds"] = {
        "schema_version": IMPACT_PREVIEW_RESPONSE_BOUNDS_SCHEMA_VERSION,
        "max_serialized_bytes": max_serialized_bytes,
        "original_serialized_bytes": original_serialized_bytes,
        "serialized_bytes": 0,
        "truncated": truncated,
        "strategy": strategy,
        "reason": "serialized_response_budget" if truncated else "within_serialized_response_budget",
        "raw_source_returned": False,
    }
    return bounded


def _finalize_impact_preview_response_bounds(response: dict[str, Any]) -> dict[str, Any]:
    bounds = response.get("response_bounds")
    if not isinstance(bounds, dict):
        raise ValueError("impact preview response bounds are unavailable")
    for _ in range(3):
        bounds["serialized_bytes"] = _serialized_json_bytes(response)
    return response


def bound_change_impact_preview_response(
    response: Mapping[str, Any],
    *,
    max_serialized_bytes: int = IMPACT_PREVIEW_RESPONSE_MAX_SERIALIZED_BYTES,
) -> dict[str, Any]:
    """Bound a producer response before the unchanged MCP REST transport guard."""

    budget = max(1_024, int(max_serialized_bytes))
    original = dict(response)
    original_size = _serialized_json_bytes(original)
    for strategy, collection_limit, mapping_limit, text_limit, depth_limit in (
        ("full", None, None, None, None),
        ("metadata_compact", 16, 32, 384, 8),
        ("metadata_minimal", 6, 16, 160, 6),
    ):
        candidate = (
            original
            if collection_limit is None
            else _compact_impact_metadata(
                original,
                collection_limit=collection_limit,
                mapping_limit=mapping_limit or 1,
                text_limit=text_limit or 1,
                depth_limit=depth_limit or 1,
            )
        )
        bounded = _with_impact_preview_response_bounds(
            candidate,
            max_serialized_bytes=budget,
            original_serialized_bytes=original_size,
            truncated=strategy != "full",
            strategy=strategy,
        )
        if _serialized_json_bytes(bounded) <= budget:
            return _finalize_impact_preview_response_bounds(bounded)

    bounded = _with_impact_preview_response_bounds(
        _impact_preview_fallback(original),
        max_serialized_bytes=budget,
        original_serialized_bytes=original_size,
        truncated=True,
        strategy="metadata_fallback",
    )
    if _serialized_json_bytes(bounded) > budget:
        raise ValueError("impact preview response bounds metadata exceeded its own budget")
    return _finalize_impact_preview_response_bounds(bounded)


def build_change_impact_preview(
    snapshot: Mapping[str, Any],
    changed_paths: Sequence[str],
    *,
    conventions: Mapping[str, Any] | None = None,
    expected_graph_digest: str | None = None,
    min_confidence: float = MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Build a bounded proposal; no source text or writes are returned/performed."""

    graph_snapshot_id = str(snapshot.get("graph_snapshot_id") or snapshot.get("snapshot_id") or "")
    graph_digest = str(snapshot.get("graph_digest") or "")
    if not graph_snapshot_id or not graph_digest:
        raise ChangeImpactError("completed graph snapshot with digest is required")
    if expected_graph_digest and expected_graph_digest != graph_digest:
        raise ChangeImpactError("graph digest changed; refresh index and graph before edit preflight")
    current_graph_digest = str(snapshot.get("current_graph_digest") or "")
    graph_stale = bool(snapshot.get("stale") or (current_graph_digest and current_graph_digest != graph_digest))
    paths = sorted({_path(item) for item in list(changed_paths)[:MAX_CHANGED_PATHS] if str(item or "").strip()})
    nodes = list(snapshot.get("nodes") or [])
    edges = list(snapshot.get("edges") or [])
    parse_results = list(snapshot.get("parse_results") or [])
    parse_errors = [item for item in parse_results if str(item.get("status")) == "error"]
    node_by_id = {_node_key(node): node for node in nodes if _node_key(node)}
    path_nodes = [node for node in nodes if str(node.get("path") or "").replace("\\", "/") in paths]
    path_node_ids = {_node_key(node) for node in path_nodes}
    adjacency: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for edge in edges:
        if str(edge.get("edge_kind") or "") not in IMPACT_EDGE_KINDS:
            continue
        source = str(edge.get("source_node_id") or "")
        target = str(edge.get("target_node_id") or "")
        if source and target:
            adjacency[source].append(edge)
            adjacency[target].append({**edge, "source_node_id": target, "target_node_id": source})
    impacted_ids = set(path_node_ids)
    queue: deque[tuple[str, int]] = deque((item, 0) for item in sorted(path_node_ids))
    while queue and len(impacted_ids) < MAX_IMPACT_NODES:
        current, depth = queue.popleft()
        if depth >= 3:
            continue
        for edge in sorted(adjacency.get(current, []), key=lambda item: str(item.get("stable_key") or "")):
            target = str(edge.get("target_node_id") or "")
            if target in node_by_id and target not in impacted_ids:
                impacted_ids.add(target)
                queue.append((target, depth + 1))
                if len(impacted_ids) >= MAX_IMPACT_NODES:
                    break
    impacted_nodes = [node_by_id[item] for item in sorted(impacted_ids) if item in node_by_id]
    selected_tests = sorted(
        {
            str(node.get("path") or "")
            for node in impacted_nodes
            if node.get("node_kind") == "test" or str(node.get("path") or "").replace("\\", "/").startswith("tests/")
        }
    )[:MAX_TEST_PATHS]
    patterns, conflicts, conventions_stale, low_evidence = _patterns(conventions)
    best_examples = sorted(
        [example for card in patterns for example in list((card.get("evidence") or {}).get("path_hashes", {}).keys())],
    )[:8]
    covered_paths = {str(node.get("path") or "").replace("\\", "/") for node in path_nodes}
    coverage = bool(paths) and set(paths).issubset(covered_paths) and not parse_errors
    low_confidence = bool(patterns) and any(
        float(item.get("confidence") or 0.0) < float(min_confidence)
        or float(item.get("freshness_score") or 0.0) < float(min_confidence)
        for item in patterns
    )
    confidence = round(min(1.0, (0.85 if coverage else 0.25) * (0.75 if (conventions_stale or graph_stale) else 1.0) * (0.75 if conflicts else 1.0) * (0.75 if low_confidence else 1.0)), 4)
    ready = bool(coverage and not conventions_stale and not graph_stale and not conflicts and not low_confidence and confidence >= float(min_confidence))
    what_would_help: list[str] = []
    if not paths:
        what_would_help.append("provide at least one repository-relative changed path")
    if not path_nodes:
        what_would_help.append("refresh WI-01/WI-02 index and graph for changed paths")
    elif not set(paths).issubset(covered_paths):
        what_would_help.append("index every changed path before edit preflight")
    if parse_errors:
        what_would_help.append("resolve parser errors before edit preflight")
    if conventions_stale:
        what_would_help.append("rebuild convention cards from the current graph")
    if graph_stale:
        what_would_help.append("refresh the graph because the current graph digest differs")
    if conflicts:
        what_would_help.append("review conflicting or rejected convention cards")
    if low_confidence:
        what_would_help.append("review low-confidence or stale convention evidence")
    core = {
        "project": str(snapshot.get("project") or "blackholememory"),
        "graph_snapshot_id": graph_snapshot_id,
        "graph_digest": graph_digest,
        "changed_paths": paths,
        "architecture_map": _architecture_map(impacted_nodes, edges),
        "impact": [{"path": str(node.get("path") or ""), "node_kind": node.get("node_kind"), "name": node.get("name"), "language": node.get("language"), "stable_key": node.get("stable_key")} for node in impacted_nodes[:MAX_IMPACT_NODES]],
        "patterns": patterns[:16],
        "bestExample": best_examples,
        "selectedTests": selected_tests,
        "whatWouldHelp": what_would_help,
        "conflicts": conflicts,
        "confidence": confidence,
        "stale": bool(conventions_stale or graph_stale),
        "graph_stale": graph_stale,
        "low_confidence": low_confidence,
        "low_evidence": low_evidence,
        "ready": ready,
    }
    return {
        "schema_version": CHANGE_IMPACT_SCHEMA_VERSION,
        "preview_digest": _sha256(core),
        **core,
        "decision_card": {
            "ready": ready,
            "patterns": patterns[:16],
            "bestExample": best_examples,
            "impact": core["impact"],
            "selectedTests": selected_tests,
            "whatWouldHelp": what_would_help,
            "confidence": confidence,
            "stale": bool(conventions_stale or graph_stale),
            "graph_stale": graph_stale,
            "low_confidence": low_confidence,
            "conflicts": conflicts,
        },
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_mem0": False, "model_started": False, "auto_apply": False, "authority": "proposal-only"},
        "gates": {"graph_digest_bound": True, "coverage_complete": coverage, "stale_rejected": not (conventions_stale or graph_stale), "conflicts_rejected": not conflicts, "low_confidence_rejected": not low_confidence, "human_review_required": True},
    }

__all__ = ["bound_change_impact_preview_response", "build_change_impact_preview"]
