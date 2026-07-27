"""Bounded proposal-only resolution of literal Bicep module targets.

The resolver operates exclusively on an already indexed graph snapshot.  It
never opens a module, invokes the Bicep compiler, evaluates expressions or
promotes an inferred edge.  Missing and ambiguous targets are first-class
results so an operator can decide whether a follow-up change is safe.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


BICEP_MODULE_RESOLUTION_SCHEMA_VERSION = "bhm.bicep-module-resolution.v1"
MAX_BICEP_MODULE_RESOLUTION_ITEMS = 256


def _digest(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(list(rows), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _candidates(source_path: str, target: str, file_paths: set[str]) -> list[str]:
    raw = str(target or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw:
        return []
    base = PurePosixPath(source_path).parent
    candidate = PurePosixPath(raw) if raw.startswith(".") else PurePosixPath(raw)
    joined = str(PurePosixPath(base / candidate)) if raw.startswith(".") else str(candidate)
    normalized = str(PurePosixPath(joined))
    matches = {path for path in file_paths if path == normalized or path.endswith(f"/{normalized}")}
    if not matches and not normalized.casefold().endswith(".bicep"):
        with_suffix = f"{normalized}.bicep"
        matches = {path for path in file_paths if path == with_suffix or path.endswith(f"/{with_suffix}")}
    return sorted(matches)


def build_bicep_module_resolution(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    *,
    max_items: int = 64,
) -> dict[str, Any]:
    """Return deterministic resolution proposals for literal Bicep modules."""

    limit = max(1, min(int(max_items), MAX_BICEP_MODULE_RESOLUTION_ITEMS))
    node_by_id = {str(node.get("node_id") or ""): node for node in nodes if str(node.get("node_id") or "")}
    file_paths = {str(node.get("path") or "") for node in nodes if str(node.get("node_kind") or "") == "file" and str(node.get("path") or "")}
    file_node_ids: dict[str, str] = {}
    for node in nodes:
        if str(node.get("node_kind") or "") == "file" and str(node.get("path") or ""):
            file_node_ids.setdefault(str(node.get("path")), str(node.get("node_id") or ""))
    rows: list[dict[str, Any]] = []
    for edge in sorted(edges, key=lambda item: (str(item.get("source_node_id") or ""), int(item.get("line") or 0), str(item.get("target_node_id") or ""))):
        source = node_by_id.get(str(edge.get("source_node_id") or "")) or {}
        target_node = node_by_id.get(str(edge.get("target_node_id") or "")) or {}
        attrs = edge.get("attributes") if isinstance(edge.get("attributes"), Mapping) else {}
        target_attrs = target_node.get("attributes") if isinstance(target_node.get("attributes"), Mapping) else {}
        target = str(attrs.get("module_target") or target_attrs.get("module_target") or "").strip()
        if not target or str(source.get("language") or "").casefold() != "bicep":
            continue
        candidates = _candidates(str(source.get("path") or ""), target, file_paths)
        status = "resolved" if len(candidates) == 1 else "ambiguous" if len(candidates) > 1 else "unresolved"
        rows.append(
            {
                "source_node_id": str(edge.get("source_node_id") or ""),
                "source_path": str(source.get("path") or ""),
                "source_name": str(source.get("name") or ""),
                "target_literal": target[:240],
                "target_paths": candidates[:16],
                "target_node_id": file_node_ids.get(candidates[0], "") if status == "resolved" else "",
                "line": int(edge.get("line") or 1),
                "resolution_status": status,
                "confidence": 0.95 if status == "resolved" else 0.45 if status == "ambiguous" else 0.2,
                "proposal_only": True,
                "evidence_class": "bicep-module-literal",
            }
        )
        if len(rows) >= limit:
            break
    return {
        "schema_version": BICEP_MODULE_RESOLUTION_SCHEMA_VERSION,
        "proposals": rows,
        "count": len(rows),
        "resolved_count": sum(item["resolution_status"] == "resolved" for item in rows),
        "unresolved_count": sum(item["resolution_status"] == "unresolved" for item in rows),
        "ambiguous_count": sum(item["resolution_status"] == "ambiguous" for item in rows),
        "digest": _digest(rows),
        "limits": {"max_items": limit, "max_candidates_per_target": 16},
        "execution": {
            "authority": "proposal",
            "proposal_only": True,
            "read_only": True,
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "raw_source_returned": False,
            "compiler_or_lsp": False,
            "network": False,
        },
    }


__all__ = ["BICEP_MODULE_RESOLUTION_SCHEMA_VERSION", "build_bicep_module_resolution"]
