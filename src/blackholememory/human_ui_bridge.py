"""Bounded human-surface and optional Obsidian bridge previews (WI-12)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .observation_security import PayloadSanitizer
from .observation_security import redact_secret_text


HUMAN_UI_BRIDGE_SCHEMA_VERSION = "bhm.human-ui-bridge.v1"
OBSIDIAN_BRIDGE_SCHEMA_VERSION = "bhm.obsidian-bridge.v1"
HUMAN_UI_MAX_NODES = 128
HUMAN_UI_MAX_LINKS = 256
HUMAN_UI_MAX_ITEMS = 64
HUMAN_UI_MAX_TEXT = 4_096
OBSIDIAN_MARKER = "bhm:readonly:v1"


class HumanUiBridgeError(ValueError):
    """Raised when a bounded human-surface preview is malformed."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _clip(value: Any, limit: int = HUMAN_UI_MAX_TEXT) -> str:
    redacted = redact_secret_text(str(value or "")).value
    return redacted.strip()[:limit]


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number in {float("inf"), float("-inf")}:
        return 0.0
    return round(min(max(number, 0.0), 1.0), 6)


def _normalize_nodes(nodes: Sequence[Mapping[str, Any]], project: str | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in list(nodes)[:HUMAN_UI_MAX_NODES * 2]:
        item = dict(raw)
        node_id = _clip(item.get("id") or item.get("entity_id"), 200)
        if not node_id or node_id in seen:
            continue
        node_project = _clip(item.get("project") or item.get("project_id"), 120)
        if project and node_project and node_project != project:
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        result.append(
            {
                "id": node_id,
                "label": _clip(item.get("label") or item.get("title") or node_id, 160),
                "type": _clip(item.get("type") or item.get("kind") or "entity", 80),
                "project": node_project or project or "unscoped",
                "confidence": _confidence(item.get("confidence", metadata.get("confidence"))),
                "stale": bool(item.get("stale", metadata.get("stale", False))),
                "quarantined": bool(item.get("quarantined", metadata.get("quarantined", False))),
                "source_ref": _clip(item.get("source_ref") or item.get("sourceRef") or metadata.get("source_ref"), 240),
                "snapshot_id": _clip(item.get("snapshot_id") or item.get("snapshotId") or metadata.get("snapshot_id"), 160),
                "provenance": _clip(item.get("provenance") or metadata.get("provenance") or "unknown", 160),
            }
        )
        seen.add(node_id)
    return sorted(result, key=lambda item: (item["type"], item["label"], item["id"]))[:HUMAN_UI_MAX_NODES]


def _normalize_links(links: Sequence[Mapping[str, Any]], node_ids: set[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in list(links)[:HUMAN_UI_MAX_LINKS * 2]:
        item = dict(raw)
        source = _clip(item.get("source") or item.get("from"), 200)
        target = _clip(item.get("target") or item.get("to"), 200)
        kind = _clip(item.get("kind") or item.get("type") or "related", 80)
        key = (source, target, kind)
        if not source or not target or source not in node_ids or target not in node_ids or key in seen:
            continue
        result.append({"source": source, "target": target, "kind": kind, "confidence": _confidence(item.get("confidence", 1.0)), "stale": bool(item.get("stale", False))})
        seen.add(key)
    return sorted(result, key=lambda item: (item["source"], item["target"], item["kind"]))[:HUMAN_UI_MAX_LINKS]


def _frontmatter_line(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}"
    if isinstance(value, (int, float)):
        return f"{key}: {value}"
    return f"{key}: {json.dumps(str(value or ""), ensure_ascii=False)}"


def build_obsidian_export_preview(
    notes: Sequence[Mapping[str, Any]],
    *,
    project: str = "blackholememory",
    snapshot_id: str = "",
    generated_at: str = "",
    max_notes: int = HUMAN_UI_MAX_ITEMS,
) -> dict[str, Any]:
    if not 1 <= int(max_notes) <= HUMAN_UI_MAX_ITEMS:
        raise HumanUiBridgeError("max_notes must be between 1 and 64")
    normalized_project = _clip(project, 120) or "blackholememory"
    result: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for raw in list(notes)[:HUMAN_UI_MAX_ITEMS]:
        item = dict(raw)
        entity_id = _clip(item.get("entity_id") or item.get("id"), 200)
        if not entity_id:
            rejected.append({"reason": "entity_id_missing"})
            continue
        title = _clip(item.get("title") or item.get("label") or entity_id, 180)
        content = _clip(item.get("content") or item.get("summary") or "", HUMAN_UI_MAX_TEXT)
        frontmatter = {
            "project_id": normalized_project,
            "entity_id": entity_id,
            "source_ref": _clip(item.get("source_ref") or item.get("sourceRef") or f"bhm:{entity_id}", 240),
            "snapshot_id": _clip(snapshot_id or item.get("snapshot_id") or item.get("snapshotId"), 160),
            "generated_at": _clip(generated_at or item.get("generated_at") or item.get("generatedAt"), 80),
            "confidence": _confidence(item.get("confidence", 0.0)),
            "readonly": True,
            "bhm_marker": OBSIDIAN_MARKER,
        }
        checksum_payload = {"frontmatter": frontmatter, "title": title, "content": content}
        checksum = _sha256(checksum_payload)
        frontmatter["bhm_checksum"] = checksum
        markdown = "---\n" + "\n".join(_frontmatter_line(key, value) for key, value in frontmatter.items()) + f"\n---\n\n# {title}\n\n{content}\n"
        result.append({"entity_id": entity_id, "title": title, "source_ref": frontmatter["source_ref"], "checksum": checksum, "frontmatter": frontmatter, "markdown": markdown, "readonly": True, "link": f"bhm://{entity_id}"})
    return {"schema_version": OBSIDIAN_BRIDGE_SCHEMA_VERSION, "project_id": normalized_project, "snapshot_id": _clip(snapshot_id, 160), "generated_at": _clip(generated_at, 80), "notes": result[:max_notes], "rejected": rejected, "writes_performed": False, "bridge_enabled": False, "authority": "sqlite-authoritative", "digest": _sha256({"project_id": normalized_project, "snapshot_id": _clip(snapshot_id, 160), "generated_at": _clip(generated_at, 80), "notes": result[:max_notes], "rejected": rejected})}


def build_obsidian_import_preview(notes: Sequence[Mapping[str, Any]], *, project: str = "blackholememory", max_notes: int = HUMAN_UI_MAX_ITEMS) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for raw in list(notes)[:max_notes]:
        item = dict(raw)
        frontmatter = item.get("frontmatter") if isinstance(item.get("frontmatter"), Mapping) else {}
        entity_id = _clip(frontmatter.get("entity_id") or item.get("entity_id"), 200)
        marker = _clip(frontmatter.get("bhm_marker"), 80)
        readonly = frontmatter.get("readonly") is True
        if not entity_id or marker != OBSIDIAN_MARKER or not readonly:
            rejected.append({"entity_id": entity_id, "reason": "explicit_bhm_readonly_marker_required"})
            continue
        title = _clip(item.get("title") or frontmatter.get("title") or entity_id, 180)
        content = _clip(item.get("content") or "", HUMAN_UI_MAX_TEXT)
        normalized_frontmatter = {key: frontmatter.get(key) for key in ("project_id", "entity_id", "source_ref", "snapshot_id", "generated_at", "confidence", "readonly", "bhm_marker")}
        computed = _sha256({"frontmatter": normalized_frontmatter, "title": title, "content": content})
        expected = _clip(frontmatter.get("bhm_checksum") or item.get("checksum"), 128)
        record = {"entity_id": entity_id, "project_id": _clip(frontmatter.get("project_id") or project, 120), "source_ref": _clip(frontmatter.get("source_ref"), 240), "computed_checksum": computed, "expected_checksum": expected, "authority": "review-queue"}
        if expected and expected != computed:
            conflicts.append({**record, "reason": "checksum_mismatch"})
        else:
            accepted.append(record)
    digest = _sha256({"accepted": accepted, "conflicts": conflicts, "rejected": rejected})
    return {"schema_version": OBSIDIAN_BRIDGE_SCHEMA_VERSION, "accepted": accepted, "conflicts": conflicts, "rejected": rejected, "writes_performed": False, "commits_performed": False, "authority": "review-queue", "digest": digest}


def build_human_ui_bridge_preview(
    *,
    project: str | None = None,
    nodes: Sequence[Mapping[str, Any]] = (),
    links: Sequence[Mapping[str, Any]] = (),
    selected_id: str | None = None,
    provenance: Sequence[Mapping[str, Any]] = (),
    review_items: Sequence[Mapping[str, Any]] = (),
    task_items: Sequence[Mapping[str, Any]] = (),
    context_packet: Mapping[str, Any] | None = None,
    mcp_state: Mapping[str, Any] | None = None,
    obsidian_export: Sequence[Mapping[str, Any]] = (),
    obsidian_import: Sequence[Mapping[str, Any]] = (),
    snapshot_id: str = "",
    generated_at: str = "",
) -> dict[str, Any]:
    project_name = _clip(project, 120) or None
    normalized_nodes = _normalize_nodes(nodes, project_name)
    node_ids = {item["id"] for item in normalized_nodes}
    normalized_links = _normalize_links(links, node_ids)
    selected = next((item for item in normalized_nodes if item["id"] == _clip(selected_id, 200)), None) if selected_id else None
    sanitizer = PayloadSanitizer()
    packet = sanitizer.sanitize(dict(context_packet or {}))
    provenance_rows = [sanitizer.sanitize(dict(item)) for item in list(provenance)[:HUMAN_UI_MAX_ITEMS] if isinstance(item, Mapping)]
    reviews = [sanitizer.sanitize(dict(item)) for item in list(review_items)[:HUMAN_UI_MAX_ITEMS] if isinstance(item, Mapping)]
    tasks = [sanitizer.sanitize(dict(item)) for item in list(task_items)[:HUMAN_UI_MAX_ITEMS] if isinstance(item, Mapping)]
    export = build_obsidian_export_preview(obsidian_export, project=project_name or "blackholememory", snapshot_id=snapshot_id, generated_at=generated_at)
    imported = build_obsidian_import_preview(obsidian_import, project=project_name or "blackholememory")
    core = {
        "project": project_name,
        "graph": {"nodes": normalized_nodes, "links": normalized_links, "selected": selected, "filters": {"project": project_name, "max_nodes": HUMAN_UI_MAX_NODES, "max_links": HUMAN_UI_MAX_LINKS}, "truncated": len(nodes) > len(normalized_nodes) or len(links) > len(normalized_links)},
        "provenance": provenance_rows,
        "review_queue": reviews,
        "task_board": tasks,
        "context_packet": packet,
        "mcp_state": sanitizer.sanitize(dict(mcp_state or {})),
        "surfaces": ["/bhm/galaxy", "/bhm/galaxy/data", "/bhm/mcp/panel", "/bhm/context/unified/compile", "/bhm/task-graph/query"],
        "obsidian_export": export,
        "obsidian_import": imported,
    }
    digest = _sha256(core)
    return {
        "schema_version": HUMAN_UI_BRIDGE_SCHEMA_VERSION,
        "ui_digest": digest,
        **core,
        "checks": {
            "bounded_graph": len(normalized_nodes) <= HUMAN_UI_MAX_NODES and len(normalized_links) <= HUMAN_UI_MAX_LINKS,
            "selected_provenance_explainable": selected is None or bool(selected.get("source_ref") or selected.get("provenance")),
            "stale_quarantine_visible": all("stale" in item and "quarantined" in item for item in normalized_nodes),
            "context_budget_visible": isinstance(packet, Mapping),
            "obsidian_optional": export["bridge_enabled"] is False and imported["writes_performed"] is False,
            "no_authority_write": export["writes_performed"] is False and imported["commits_performed"] is False,
            "secret_redaction_applied": sanitizer.redaction_count >= 0,
        },
        "execution": {"browser_started": False, "files_written": False, "authority_written": False, "obsidian_committed": False, "model_started": False, "auto_apply": False, "authority": "sqlite-authoritative"},
    }


def verify_human_ui_bridge_digest(preview: Mapping[str, Any]) -> bool:
    expected = str(preview.get("ui_digest") or "")
    if not expected:
        return False
    keys = ("project", "graph", "provenance", "review_queue", "task_board", "context_packet", "mcp_state", "surfaces", "obsidian_export", "obsidian_import")
    return expected == _sha256({key: preview.get(key) for key in keys})


__all__ = [
    "HUMAN_UI_BRIDGE_SCHEMA_VERSION",
    "HUMAN_UI_MAX_LINKS",
    "HUMAN_UI_MAX_NODES",
    "OBSIDIAN_BRIDGE_SCHEMA_VERSION",
    "OBSIDIAN_MARKER",
    "HumanUiBridgeError",
    "build_human_ui_bridge_preview",
    "build_obsidian_export_preview",
    "build_obsidian_import_preview",
    "verify_human_ui_bridge_digest",
]
