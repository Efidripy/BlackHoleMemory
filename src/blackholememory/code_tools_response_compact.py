"""Compact, deterministic projections for public code analysis responses."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _summary(value: Any) -> dict[str, Any]:
    return {"count": len(value), "digest": _digest(value), "detail": "full"}


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
