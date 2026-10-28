"""Compact, deterministic projections for public code analysis responses."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _summary(value: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"count": len(value), "digest": _digest(value), "detail": "full"}
    if isinstance(value, dict):
        # The authority/provenance markers are the proposal-only and zero-write
        # evidence; they stay visible when the bulky body is replaced by a digest.
        for key in ("execution", "provenance", "binding", "schema_version", "status"):
            if isinstance(value.get(key), (dict, str, bool, int)):
                summary[key] = value[key]
    return summary


def compact_response(value: Any, *, offset: int, limit: int, full: bool = False) -> Any:
    """Keep public list fields as lists, with per-list pagination metadata."""
    if isinstance(value, dict):
        result = {}
        pagination = {}
        for key, item in value.items():
            if isinstance(item, list) and (key in {"layers", "dependencies", "hotspots", "added", "changed", "removed", "renamed"} or (full and key == "languages")):
                total = int(value.get(f"{key}_count", len(item)))
                page = item[offset:offset + limit]
                result[key] = page
                pagination[key] = {"total": total, "next_offset": offset + len(page) if offset + len(page) < total and offset + len(page) < len(item) else None, "offset": offset, "limit": limit, "digest": _digest(item)}
                if not full and len(page) < total:
                    pagination[key]["truncated"] = True
            elif not full and key in {"languages", "intelligence", "explain_receipt", "architecture_memory", "histograms", "histogram"} and isinstance(item, (dict, list)):
                result[key] = _summary(item)
            else:
                result[key] = compact_response(item, offset=offset, limit=limit, full=full)
        if pagination:
            result["pagination"] = pagination
        return result
    if isinstance(value, list):
        if not full and len(value) > 16:
            return _summary(value)
        return [compact_response(item, offset=offset, limit=limit, full=full) for item in value]
    return value


def bound_coverage_response(response: dict[str, Any]) -> dict[str, Any]:
    """Bound compact coverage JSON, retaining continuation metadata for pages."""
    def size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=True, default=str).encode("utf-8"))

    def pages(value: Any) -> list[tuple[dict[str, Any], str, dict[str, Any]]]:
        found = []
        if isinstance(value, dict):
            for key, metadata in value.get("pagination", {}).items():
                if isinstance(value.get(key), list) and value[key]:
                    found.append((value, key, metadata))
            for key, item in value.items():
                if key != "pagination":
                    found.extend(pages(item))
        elif isinstance(value, list):
            for item in value:
                found.extend(pages(item))
        return found

    budget = 19_000
    while size(response) > budget:
        candidates = pages(response)
        if not candidates:
            break
        container, key, metadata = max(candidates, key=lambda page: size(page[0][page[1]]))
        page = container[key]
        del page[len(page) // 2:]
        metadata["truncated"] = True
        metadata["next_offset"] = metadata["offset"] + len(page)
        response["truncated"] = True
    if size(response) > budget:
        for key in ("index", "summary", "parser_capabilities"):
            if isinstance(response.get(key), (dict, list)):
                response[key] = _summary(response[key])
                response["truncated"] = True
            if size(response) <= budget:
                break
    if size(response) > budget:
        # Unbounded scalar values or mapping keys can also exceed the budget.
        response = {"operation": "coverage", "detail": "compact", "truncated": True}
    return response
