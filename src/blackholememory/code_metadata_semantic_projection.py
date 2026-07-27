"""Deterministic, metadata-only semantic projection planning for code graphs.

The planner deliberately has no provider or Qdrant side effects.  An
operator script may submit its bounded payload through the existing
SQLite-authoritative memory upsert/outbox path after a capability check.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .mem0_adapter import local_collection_name


SCHEMA_VERSION = "bhm.code-metadata-semantic-projection.v1"
DEFAULT_MAX_FILES = 128
MAX_PATH_CHARS = 512
MAX_SUMMARY_CHARS = 2_000
_SAFE_PROJECT = re.compile(r"[^A-Za-z0-9_.-]+")


class CodeMetadataProjectionError(ValueError):
    """Raised when a metadata projection candidate violates its contract."""


def _safe_project(value: str) -> str:
    project = _SAFE_PROJECT.sub("-", str(value or "").strip()).strip("-._")
    if not project:
        raise CodeMetadataProjectionError("project must be non-empty")
    return project[:128].casefold()


def _safe_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path or len(path) > MAX_PATH_CHARS or "\x00" in path:
        raise CodeMetadataProjectionError("metadata path is invalid")
    if path.startswith("/") or "../" in f"{path}/" or path == "..":
        raise CodeMetadataProjectionError("metadata path escapes repository root")
    return path


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_metadata_projection_items(
    rows: Iterable[Mapping[str, Any]],
    *,
    project: str,
    graph_snapshot_id: str,
    graph_digest: str,
    max_files: int = DEFAULT_MAX_FILES,
) -> list[dict[str, Any]]:
    """Build idempotent metadata-only memory payloads from graph file rows.

    Callers provide one row per file with ``path``, ``language``,
    ``node_count`` and optional ``node_kinds``.  Source content, signatures,
    operands and external references are rejected rather than copied.
    """

    canonical_project = _safe_project(project)
    snapshot = str(graph_snapshot_id or "").strip()
    digest = str(graph_digest or "").strip()
    if not snapshot or not digest or len(digest) != 64:
        raise CodeMetadataProjectionError("graph snapshot identity is incomplete")
    cap = max(1, min(int(max_files), DEFAULT_MAX_FILES))
    normalized: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = _safe_path(row.get("path"))
        language = str(row.get("language") or "unknown").strip()[:80] or "unknown"
        try:
            node_count = max(0, min(int(row.get("node_count") or 0), 100_000))
        except (TypeError, ValueError) as exc:
            raise CodeMetadataProjectionError("node_count must be an integer") from exc
        raw_kinds = row.get("node_kinds") or row.get("kinds") or []
        if isinstance(raw_kinds, str):
            kinds = [item.strip() for item in raw_kinds.split(",") if item.strip()]
        else:
            kinds = [str(item).strip() for item in raw_kinds if str(item).strip()]
        kinds = sorted(set(kinds))[:16]
        normalized[path] = {"path": path, "language": language, "node_count": node_count, "node_kinds": kinds}
    selected = [normalized[path] for path in sorted(normalized)[:cap]]
    items: list[dict[str, Any]] = []
    for row in selected:
        path = row["path"]
        content = (
            f"code metadata project={canonical_project} path={path} "
            f"language={row['language']} node_kinds={','.join(row['node_kinds'])} "
            f"node_count={row['node_count']} graph_digest={digest}"
        )[:MAX_SUMMARY_CHARS]
        items.append(
            {
                "upsert_key": f"code-metadata:{canonical_project}:{path}",
                "content": content,
                "project": canonical_project,
                "memory_type": "knowledge",
                "concepts": ["code-metadata", "cbm-parity", "semantic-index"],
                "files": [path],
                "metadata": {
                    "provenance": "synthetic",
                    "domain": "backend",
                    "scope": "local",
                    "retention": "long-term",
                    "verification": "unverified",
                    "semantic_type": "knowledge",
                    "source_system": "bhm-code-graph",
                    "source_kind": "code-graph-metadata",
                    "graph_snapshot_id": snapshot,
                    "graph_digest": digest,
                    "raw_source_returned": False,
                },
            }
        )
    return items


def build_projection_completeness(
    rows: Iterable[Mapping[str, Any]],
    *,
    project: str,
    graph_snapshot_id: str,
    graph_digest: str,
    max_files: int = DEFAULT_MAX_FILES,
    current_graph_snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Return deterministic bounded-selection evidence before projection apply."""

    canonical_project = _safe_project(project)
    snapshot = str(graph_snapshot_id or "").strip()
    digest = str(graph_digest or "").strip()
    if not snapshot or len(digest) != 64:
        raise CodeMetadataProjectionError("graph snapshot identity is incomplete")
    current = str(current_graph_snapshot_id or "").strip()
    if current and current != snapshot:
        raise CodeMetadataProjectionError("graph snapshot is stale relative to current project pointer")
    paths: set[str] = set()
    for row in rows:
        paths.add(_safe_path(row.get("path")))
    cap = max(1, min(int(max_files), DEFAULT_MAX_FILES))
    source_count = len(paths)
    selected_count = min(source_count, cap)
    skipped_count = max(0, source_count - selected_count)
    return {
        "project": canonical_project,
        "graph_snapshot_id": snapshot,
        "graph_digest": digest,
        "max_files": cap,
        "source_row_count": source_count,
        "selected_count": selected_count,
        "skipped_count": skipped_count,
        "truncated": skipped_count > 0,
        "source_paths_sha256": _digest(sorted(paths)),
        "target_point_count": selected_count,
    }


def build_projection_plan(
    items: Iterable[Mapping[str, Any]],
    *,
    project: str,
    completeness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable side-effect-free plan and rollback identity."""

    payload = [dict(item) for item in items]
    canonical_project = _safe_project(project)
    keys = [str(item.get("upsert_key") or "") for item in payload]
    if not payload or any(not key.startswith(f"code-metadata:{canonical_project}:") for key in keys):
        raise CodeMetadataProjectionError("projection items must be non-empty and project-scoped")
    if len(keys) != len(set(keys)):
        raise CodeMetadataProjectionError("projection upsert keys must be unique")
    receipt = dict(completeness or {})
    if receipt:
        if str(receipt.get("project") or "").casefold() != canonical_project:
            raise CodeMetadataProjectionError("completeness receipt project mismatch")
        if int(receipt.get("selected_count") or 0) != len(payload):
            raise CodeMetadataProjectionError("completeness selected count mismatch")
        if bool(receipt.get("truncated")) and int(receipt.get("skipped_count") or 0) <= 0:
            raise CodeMetadataProjectionError("truncated completeness receipt is inconsistent")
    else:
        receipt = {
            "project": canonical_project,
            "source_row_count": len(payload),
            "selected_count": len(payload),
            "skipped_count": 0,
            "truncated": False,
            "max_files": len(payload),
            "target_point_count": len(payload),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "project": canonical_project,
        "item_count": len(payload),
        "payload_sha256": _digest(payload),
        "upsert_keys": keys,
        "target_collection": local_collection_name(canonical_project),
        "completeness": receipt,
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "raw_source_returned": False,
            "provider_calls": False,
            "autonomous_apply": False,
        },
        "rollback": {
            "delete_upsert_keys": keys,
            "delete_collection": local_collection_name(canonical_project),
            "requires_sqlite_backup": True,
            "requires_admin_capability": True,
        },
    }


__all__ = [
    "CodeMetadataProjectionError",
    "DEFAULT_MAX_FILES",
    "SCHEMA_VERSION",
    "build_metadata_projection_items",
    "build_projection_completeness",
    "build_projection_plan",
]
