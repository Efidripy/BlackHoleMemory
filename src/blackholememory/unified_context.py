"""Unified, bounded context assembly for WI-08.

The existing ``compile_context`` remains the packing primitive.  This module
adds deterministic source-aware interleaving for memory, code, conventions,
tasks, docs and ops while preserving the public MCP contract.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .code_graph_query import query_code_graph
from .context_compiler import DEFAULT_CONTEXT_TOKEN_BUDGET
from .context_compiler import MAX_CONTEXT_ITEM_CHARS
from .context_compiler import MAX_CONTEXT_TOKEN_BUDGET
from .context_compiler import compile_context
from .convention_memory import ConventionMemoryError
from .convention_memory import SQLiteConventionMemoryStore


UNIFIED_CONTEXT_SCHEMA_VERSION = "bhm.unified-context.v1"
UNIFIED_CONTEXT_MAX_ITEMS_PER_SOURCE = 32
UNIFIED_CONTEXT_SOURCES = ("code", "conventions", "tasks", "docs", "ops", "memory")


class UnifiedContextError(ValueError):
    """Raised when unified context input cannot be safely bounded."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_strings(value: Any, limit: int = 8) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _item_id(item: Mapping[str, Any], source_kind: str, rank: int) -> str:
    return str(item.get("id") or item.get("source_id") or f"{source_kind}-{rank}").strip()[:240]


def _classify_memory_item(item: Mapping[str, Any]) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    explicit = str(item.get("source_kind") or metadata.get("source_kind") or "").casefold()
    if explicit in {"code", "conventions", "tasks", "docs", "ops", "memory"}:
        return explicit
    memory_type = str(item.get("memory_type") or item.get("type") or metadata.get("memory_type") or "").casefold()
    files = " ".join(_bounded_strings(item.get("files") or metadata.get("files"), limit=8)).casefold()
    if "task" in memory_type or "task" in files:
        return "tasks"
    if "doc" in files or "/readme" in files or "docs/" in files:
        return "docs"
    if "ops/" in files or "runbook" in files or "incident" in memory_type:
        return "ops"
    return "memory"


def classify_context_item(item: Mapping[str, Any]) -> str:
    """Classify a retrieved item into one bounded unified source channel."""

    return _classify_memory_item(item)


def _normalize_item(item: Mapping[str, Any], *, source_kind: str, rank: int, project: str) -> dict[str, Any]:
    metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), Mapping) else {}
    source_refs = _bounded_strings(item.get("source_refs") if item.get("source_refs") is not None else metadata.get("source_refs"))
    files = _bounded_strings(item.get("files") if item.get("files") is not None else metadata.get("files"))
    metadata.update(
        {
            "source_kind": source_kind,
            "source_refs": source_refs,
            "files": files,
        }
    )
    return {
        "id": _item_id(item, source_kind, rank),
        "title": str(item.get("title") or item.get("name") or item.get("id") or f"{source_kind}-{rank}").strip()[:240],
        "content": str(item.get("content") or item.get("memory") or item.get("statement") or "").strip()[:MAX_CONTEXT_ITEM_CHARS],
        "project": str(item.get("project") or project).strip()[:120],
        "score": float(item.get("score") or item.get("confidence") or 0.0),
        "source_id": _item_id(item, source_kind, rank),
        "source_system": str(item.get("source_system") or "bhm").strip()[:80],
        "context_origin": str(item.get("context_origin") or ("PROPOSAL" if source_kind == "conventions" and item.get("status") == "proposal" else "LOCAL")).strip()[:40],
        "source_refs": source_refs,
        "files": files,
        "metadata": metadata,
    }


def _interleave(items_by_source: Mapping[str, Sequence[Mapping[str, Any]]], *, project: str, max_items_per_source: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    bounded: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for source in UNIFIED_CONTEXT_SOURCES:
        raw = list(items_by_source.get(source) or [])[:max_items_per_source]
        bounded[source] = [_normalize_item(item, source_kind=source, rank=index, project=project) for index, item in enumerate(raw, start=1)]
        counts[source] = len(bounded[source])
    result: list[dict[str, Any]] = []
    for index in range(max((len(items) for items in bounded.values()), default=0)):
        for source in UNIFIED_CONTEXT_SOURCES:
            if index < len(bounded[source]):
                result.append(bounded[source][index])
    return result, counts


def compile_unified_context(
    items_by_source: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    project: str,
    query: str = "",
    token_budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET,
    max_item_chars: int = MAX_CONTEXT_ITEM_CHARS,
    max_items_per_source: int = UNIFIED_CONTEXT_MAX_ITEMS_PER_SOURCE,
) -> dict[str, Any]:
    """Interleave bounded source channels and compile one cited context."""

    project = str(project or "").strip()[:120]
    if not project:
        raise UnifiedContextError("project is required")
    max_items = max(1, min(int(max_items_per_source), UNIFIED_CONTEXT_MAX_ITEMS_PER_SOURCE))
    items, source_counts = _interleave(items_by_source, project=project, max_items_per_source=max_items)
    compiled = compile_context(items, token_budget=max(1, min(int(token_budget), MAX_CONTEXT_TOKEN_BUDGET)), max_item_chars=max(80, int(max_item_chars)))
    included_sources = Counter(str((citation.get("provenance") or {}).get("source_kind") or "memory") for citation in compiled["citations"])
    source_digests = {
        source: _sha256(_canonical_json([_normalize_item(item, source_kind=source, rank=index, project=project) for index, item in enumerate(list(items_by_source.get(source) or [])[:max_items], start=1)]))
        for source in UNIFIED_CONTEXT_SOURCES
    }
    digest_payload = {
        "schema_version": UNIFIED_CONTEXT_SCHEMA_VERSION,
        "project": project,
        "query": str(query or "")[:480],
        "source_digests": source_digests,
        "text": compiled["text"],
        "citations": compiled["citations"],
        "omissions": compiled["omissions"],
    }
    return {
        "schema_version": UNIFIED_CONTEXT_SCHEMA_VERSION,
        "query": str(query or "")[:480],
        "project": project,
        "context": compiled["text"],
        "citations": compiled["citations"],
        "provenance": compiled["provenance"],
        "omissions": compiled["omissions"],
        "token_budget": compiled["token_budget"],
        "estimated_tokens": compiled["estimated_tokens"],
        "truncated": compiled["truncated"],
        "sources": {
            "requested": source_counts,
            "included": dict(sorted(included_sources.items())),
            "coverage": {source: int(included_sources.get(source, 0)) for source in UNIFIED_CONTEXT_SOURCES},
        },
        "source_digests": source_digests,
        "response_digest": _sha256(_canonical_json(digest_payload)),
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "writes_mem0": False,
            "writes_retrieval_feedback": False,
            "model_started": False,
            "raw_source_returned": False,
            "public_mcp_changed": False,
        },
    }


def code_graph_context_items(
    database_path: str | Path,
    *,
    project: str,
    root_id: str,
    query: str,
    operation: str = "symbol",
    depth: int = 2,
    limit: int = 16,
    time_budget_ms: float = 500.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    response = query_code_graph(database_path, project=project, root_id=root_id, operation=operation, query=query, depth=depth, limit=limit, max_tokens=8_192, time_budget_ms=time_budget_ms)
    items: list[dict[str, Any]] = []
    for node in response.get("nodes") or []:
        path = str(node.get("path") or "")
        name = str(node.get("qualified_name") or node.get("name") or node.get("stable_key") or "")
        signature = str(node.get("signature") or "")[:480]
        content = f"{node.get('node_kind', 'node')}: {name}"
        if path:
            content += f" @ {path}"
        if signature:
            content += f" ({signature})"
        source_ref = str(node.get("source_ref") or "")
        items.append(
            {
                "id": str(node.get("node_id") or node.get("stable_key") or ""),
                "title": f"Code {node.get('node_kind', 'node')}: {name}",
                "content": content,
                "project": project,
                "score": 0.9,
                "source_id": str(node.get("node_id") or ""),
                "source_system": "bhm-code-graph",
                "source_refs": [source_ref] if source_ref else [],
                "files": [path] if path else [],
                "metadata": {
                    "source_kind": "code",
                    "graph_snapshot_id": response.get("snapshot_id"),
                    "graph_digest": response.get("graph_digest"),
                    "source_refs": [source_ref] if source_ref else [],
                    "files": [path] if path else [],
                    "node_kind": node.get("node_kind"),
                    "stable_key": node.get("stable_key"),
                    "stale": response.get("stale"),
                },
            }
        )
    return items, {"snapshot_id": response.get("snapshot_id"), "graph_digest": response.get("graph_digest"), "stale": response.get("stale"), "bounds": response.get("bounds"), "response_digest": response.get("response_digest")}


def convention_context_items(
    database_path: str | Path,
    *,
    project: str,
    root_id: str,
    include_proposals: bool = False,
    limit: int = 16,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    store = SQLiteConventionMemoryStore(database_path)
    current = store.current_snapshot(project, root_id, include_cards=True)
    if current is None:
        return [], {"available": False, "stale": False, "convention_snapshot_id": None}
    items: list[dict[str, Any]] = []
    for card in list(current.get("cards") or [])[: max(1, min(int(limit), 32))]:
        if bool(card.get("stale")):
            continue
        if card.get("status") != "accepted" and not include_proposals:
            continue
        evidence = card.get("evidence") if isinstance(card.get("evidence"), Mapping) else {}
        status = str(card.get("status") or "proposal")
        content = f"{card.get('title')}: {card.get('statement')}"
        if card.get("rationale"):
            content += f" Rationale: {card.get('rationale')}"
        source_refs = _bounded_strings(evidence.get("source_refs"))
        files = _bounded_strings(evidence.get("related_test_paths")) + _bounded_strings(evidence.get("related_adr_paths"))
        items.append(
            {
                "id": str(card.get("card_id") or ""),
                "title": f"Convention {status}: {card.get('title')}",
                "content": content,
                "project": project,
                "score": float(card.get("confidence") or 0.0),
                "status": status,
                "source_id": str(card.get("card_id") or ""),
                "source_system": "bhm-convention-memory",
                "context_origin": "PROPOSAL" if status == "proposal" else "LOCAL",
                "source_refs": source_refs,
                "files": files,
                "metadata": {
                    "source_kind": "conventions",
                    "source_refs": source_refs,
                    "files": files,
                    "convention_snapshot_id": current.get("convention_snapshot_id"),
                    "graph_snapshot_id": current.get("graph_snapshot_id"),
                    "graph_digest": current.get("graph_digest"),
                    "status": status,
                },
            }
        )
    return items, {"available": True, "stale": False, "convention_snapshot_id": current.get("convention_snapshot_id"), "graph_snapshot_id": current.get("graph_snapshot_id"), "card_count": len(items)}


def build_unified_context_from_graph(
    database_path: str | Path,
    *,
    project: str,
    root_id: str,
    query: str,
    memory_items: Sequence[Mapping[str, Any]] = (),
    task_items: Sequence[Mapping[str, Any]] = (),
    doc_items: Sequence[Mapping[str, Any]] = (),
    ops_items: Sequence[Mapping[str, Any]] = (),
    code_operation: str = "symbol",
    include_code: bool = True,
    include_conventions: bool = True,
    include_proposals: bool = False,
    token_budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET,
    limit: int = 16,
    time_budget_ms: float = 500.0,
) -> dict[str, Any]:
    sources: dict[str, Sequence[Mapping[str, Any]]] = {
        "memory": list(memory_items),
        "tasks": list(task_items),
        "docs": list(doc_items),
        "ops": list(ops_items),
    }
    diagnostics: dict[str, Any] = {}
    if include_code:
        try:
            sources["code"], diagnostics["code"] = code_graph_context_items(database_path, project=project, root_id=root_id, query=query, operation=code_operation, limit=limit, time_budget_ms=time_budget_ms)
        except Exception as exc:
            diagnostics["code"] = {"available": False, "error": type(exc).__name__}
            sources["code"] = []
    else:
        diagnostics["code"] = {"available": False, "disabled": True}
    if include_conventions:
        try:
            sources["conventions"], diagnostics["conventions"] = convention_context_items(database_path, project=project, root_id=root_id, include_proposals=include_proposals, limit=limit)
        except ConventionMemoryError as exc:
            diagnostics["conventions"] = {"available": False, "error": type(exc).__name__}
            sources["conventions"] = []
    else:
        diagnostics["conventions"] = {"available": False, "disabled": True}
    result = compile_unified_context(sources, project=project, query=query, token_budget=token_budget, max_items_per_source=limit)
    result["diagnostics"] = diagnostics
    result["retrieval"] = {"mode": "unified-source-aware", "code_operation": code_operation, "include_code": include_code, "include_conventions": include_conventions, "include_proposals": include_proposals}
    result["response_digest"] = _sha256(_canonical_json({key: value for key, value in result.items() if key not in {"response_digest", "diagnostics"}}))
    return result


__all__ = [
    "UNIFIED_CONTEXT_SCHEMA_VERSION",
    "UNIFIED_CONTEXT_SOURCES",
    "UnifiedContextError",
    "build_unified_context_from_graph",
    "classify_context_item",
    "code_graph_context_items",
    "compile_unified_context",
    "convention_context_items",
]
