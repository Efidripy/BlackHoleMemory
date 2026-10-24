"""Deterministic, preview-only memory consolidation for the local LLM contour.

Memory Foundry is deliberately a read-only composition layer.  It turns the
existing memory detectors into bounded proposals and compact fact crystals;
it never writes memories, links, summaries, queues, or Git artifacts.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .llm_safety import sanitize_llm_value


MEMORY_FOUNDRY_SCHEMA_VERSION = "bhm.llm.memory-foundry.v1"
MEMORY_FOUNDRY_MAX_RECORDS = 128
MEMORY_FOUNDRY_MAX_PROPOSALS = 96
MEMORY_FOUNDRY_MAX_TEXT = 1200
MEMORY_FOUNDRY_MAX_TAGS = 12


class MemoryFoundryError(ValueError):
    """Raised when a Foundry preview exceeds its deterministic bounds."""


def build_memory_foundry_preview(
    records: Sequence[Mapping[str, Any]],
    *,
    project: str = "",
    duplicate_candidates: Sequence[Mapping[str, Any]] = (),
    conflict_candidates: Sequence[Mapping[str, Any]] = (),
    relation_candidates: Sequence[Mapping[str, Any]] = (),
    cross_project_records: Sequence[Mapping[str, Any]] = (),
    stale_days: int = 90,
    undo_window_seconds: int = 900,
    limit: int = 32,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a bounded proposal snapshot without mutating any authority."""

    if len(records) > MEMORY_FOUNDRY_MAX_RECORDS:
        raise MemoryFoundryError(f"records exceed limit {MEMORY_FOUNDRY_MAX_RECORDS}")
    if not 1 <= int(limit) <= MEMORY_FOUNDRY_MAX_PROPOSALS:
        raise MemoryFoundryError("limit must be between 1 and 96")
    if not 1 <= int(stale_days) <= 3650:
        raise MemoryFoundryError("stale_days must be between 1 and 3650")
    if not 1 <= int(undo_window_seconds) <= 86_400:
        raise MemoryFoundryError("undo_window_seconds must be between 1 and 86400")

    normalized_project = _clip(project, 120) or None
    clock = now or datetime.now(timezone.utc)
    compact_records = _compact_records(records, normalized_project)
    cross_project_compact = _compact_records(list(cross_project_records)[:MEMORY_FOUNDRY_MAX_RECORDS], None)
    facts = _fact_crystals(compact_records)
    proposals: list[dict[str, Any]] = []
    proposals.extend(_candidate_proposals("duplicate", duplicate_candidates, normalized_project, undo_window_seconds))
    proposals.extend(_candidate_proposals("conflict", conflict_candidates, normalized_project, undo_window_seconds))
    proposals.extend(_candidate_proposals("relation", relation_candidates, normalized_project, undo_window_seconds))
    proposals.extend(_stale_proposals(compact_records, normalized_project, stale_days, clock, undo_window_seconds))
    proposals = sorted(proposals, key=lambda item: (-float(item["score"]), item["proposal_id"]))[: int(limit)]
    summary = _project_summary(compact_records, normalized_project)
    core = {
        "project": normalized_project,
        "record_count": len(compact_records),
        "records": compact_records,
        "fact_crystals": facts,
        "super_crystal": _super_crystal(facts, summary),
        "project_summary": summary,
        "cross_project_patterns": _cross_project_patterns(cross_project_compact, normalized_project),
        "proposals": proposals,
        "stale_days": int(stale_days),
        "generated_at": clock.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    preview_digest = _sha256(_canonical_json(core))
    return {
        "schema_version": MEMORY_FOUNDRY_SCHEMA_VERSION,
        "preview_digest": preview_digest,
        **core,
        "counts": {
            "records": len(compact_records),
            "fact_crystals": len(facts),
            "proposals": len(proposals),
            "stale": sum(1 for item in proposals if item["kind"] == "stale_review"),
        },
        "mutation": {
            "preview_only": True,
            "writes_performed": False,
            "auto_apply": False,
            "requires_confirmation": True,
            "authority": "proposal",
        },
        "undo": {
            "available": True,
            "window_seconds": int(undo_window_seconds),
            "undo_token_digest": _sha256(f"{preview_digest}:{int(undo_window_seconds)}"),
            "apply_endpoint": None,
        },
    }


def verify_memory_foundry_digest(preview: Mapping[str, Any]) -> bool:
    """Verify the digest of a previously returned preview snapshot."""

    expected = str(preview.get("preview_digest") or "")
    if not expected:
        return False
    core = {
        key: preview.get(key)
        for key in (
            "project",
            "record_count",
            "records",
            "fact_crystals",
            "super_crystal",
            "project_summary",
            "cross_project_patterns",
            "proposals",
            "stale_days",
            "generated_at",
        )
    }
    return expected == _sha256(_canonical_json(core))


def _compact_records(records: Sequence[Mapping[str, Any]], project: str | None) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for raw in records:
        record = dict(raw)
        record_project = _clip(record.get("project"), 120) or None
        if project and record_project != project:
            continue
        memory_id = _clip(record.get("source_id") or record.get("id"), 180)
        if not memory_id:
            continue
        metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
        safe_project = project or record_project or "blackholememory"
        title = _safe_text(metadata.get("raw_title") or record.get("title"), safe_project, 180)
        content = _safe_text(record.get("content"), safe_project)
        tags = _bounded_strings(
            record.get("tags") or record.get("concepts") or metadata.get("tags"),
            80,
            MEMORY_FOUNDRY_MAX_TAGS,
            project=safe_project,
        )
        files = _bounded_strings(metadata.get("files") or record.get("files"), 240, 12, project=safe_project)
        compact.append(
            {
                "id": memory_id,
                "project": record_project,
                "type": _clip(record.get("memory_type") or record.get("type"), 80) or "knowledge",
                "title": title or _clip(content.splitlines()[0] if content else "", 180),
                "content_excerpt": content,
                "tags": tags,
                "files": files,
                "created_at": _clip(record.get("created_at"), 64),
                "updated_at": _clip(record.get("updated_at"), 64),
                "archived": bool(metadata.get("archived_at") or record.get("archived_at")),
                "confidence": _bounded_score(metadata.get("confidence"), default=0.5),
            }
        )
    return sorted(compact, key=lambda item: (str(item.get("updated_at") or ""), item["id"]), reverse=True)


def _fact_crystals(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        semantic_type = _semantic_type(record)
        groups.setdefault(semantic_type, []).append(record)
    crystals: list[dict[str, Any]] = []
    for semantic_type, items in sorted(groups.items()):
        tags = Counter(tag for item in items for tag in item.get("tags", []))
        ids = [str(item["id"]) for item in items[:12]]
        insight = str(items[0].get("title") or items[0].get("content_excerpt") or semantic_type)
        crystals.append(
            {
                "crystal_id": f"fact_{_sha256(f'{semantic_type}:{','.join(ids)}')[:24]}",
                "semantic_type": semantic_type,
                "core_insight": _clip(insight, 240),
                "memory_ids": ids,
                "evidence_count": len(items),
                "reusable_patterns": [tag for tag, _ in tags.most_common(6)],
                "confidence": round(sum(float(item.get("confidence") or 0.0) for item in items) / max(len(items), 1), 4),
                "authority": "proposal",
            }
        )
    return crystals


def _super_crystal(facts: Sequence[Mapping[str, Any]], summary: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "crystal_id": f"super_{_sha256(_canonical_json({'facts': facts, 'summary': summary}))[:24]}",
        "core_insight": f"{summary.get('record_count', 0)} memories grouped into {len(facts)} reusable semantic clusters",
        "fact_crystal_ids": [str(item["crystal_id"]) for item in facts],
        "dominant_tags": list(summary.get("top_tags") or [])[:8],
        "authority": "proposal",
    }


def _project_summary(records: Sequence[Mapping[str, Any]], project: str | None) -> dict[str, Any]:
    type_counts = Counter(str(item.get("type") or "knowledge") for item in records)
    tags = Counter(tag for item in records for tag in item.get("tags", []))
    return {
        "project": project,
        "record_count": len(records),
        "active_count": sum(1 for item in records if not item.get("archived")),
        "archived_count": sum(1 for item in records if item.get("archived")),
        "type_counts": dict(sorted(type_counts.items())),
        "top_tags": [tag for tag, _ in tags.most_common(8)],
        "recent_memory_ids": [str(item["id"]) for item in records[:8]],
    }


def _cross_project_patterns(records: Sequence[Mapping[str, Any]], project: str | None) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, list[str]]] = {}
    for record in records:
        record_project = str(record.get("project") or "")
        if not record_project:
            continue
        for tag in record.get("tags", []):
            groups.setdefault(str(tag), {}).setdefault(record_project, []).append(str(record["id"]))
    patterns: list[dict[str, Any]] = []
    for tag, project_map in sorted(groups.items()):
        if len(project_map) < 2:
            continue
        projects = sorted(project_map)
        memory_ids = [memory_id for name in projects for memory_id in project_map[name]][:12]
        score = round(min(1.0, 0.45 + 0.1 * len(projects)), 4)
        payload = {"tag": tag, "projects": projects, "memory_ids": memory_ids}
        patterns.append(
            {
                "pattern_id": f"pattern_{_sha256(_canonical_json(payload))[:24]}",
                "pattern": tag,
                "projects": projects,
                "memory_ids": memory_ids,
                "evidence_count": len(memory_ids),
                "score": score,
                "authority": "proposal",
                "requires_confirmation": True,
                "auto_apply": False,
            }
        )
    return sorted(patterns, key=lambda item: (-float(item["score"]), item["pattern_id"]))[:16]


def _candidate_proposals(
    kind: str,
    candidates: Sequence[Mapping[str, Any]],
    project: str | None,
    undo_window_seconds: int,
) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    for candidate in list(candidates)[:MEMORY_FOUNDRY_MAX_PROPOSALS]:
        item = dict(candidate)
        source_ids = _bounded_strings(item.get("memory_ids") or [item.get("left_id") or item.get("source_id")], 180, 4)
        target_ids = _bounded_strings([item.get("right_id") or item.get("target_id")], 180, 2)
        source_ids = [value for value in source_ids if value]
        target_ids = [value for value in target_ids if value and value not in source_ids]
        if not source_ids:
            continue
        score = _bounded_score(item.get("score"), default=0.5)
        reason = _safe_text(item.get("reason"), project or "blackholememory", 240) or f"{kind}_candidate"
        action = {"duplicate": "merge_after_review", "conflict": "resolve_after_review", "relation": "link_after_review"}.get(kind, "review")
        proposals.append(_proposal(kind, source_ids, target_ids, reason, score, action, project, undo_window_seconds))
    return proposals


def _stale_proposals(
    records: Sequence[Mapping[str, Any]],
    project: str | None,
    stale_days: int,
    now: datetime,
    undo_window_seconds: int,
) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    for record in records:
        timestamp = _parse_time(record.get("updated_at") or record.get("created_at"))
        if timestamp is None:
            continue
        age_days = max(0.0, (now.astimezone(timezone.utc) - timestamp).total_seconds() / 86_400)
        if age_days < stale_days:
            continue
        score = min(1.0, round(0.5 + age_days / max(stale_days * 4, 1), 4))
        proposals.append(
            _proposal(
                "stale_review",
                [str(record["id"])],
                [],
                f"age_days={round(age_days, 1)}",
                score,
                "refresh_or_archive_after_review",
                project,
                undo_window_seconds,
                extra={"age_days": round(age_days, 1)},
            )
        )
    return proposals


def _proposal(
    kind: str,
    source_ids: Sequence[str],
    target_ids: Sequence[str],
    reason: str,
    score: float,
    action: str,
    project: str | None,
    undo_window_seconds: int,
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {"kind": kind, "project": project, "source_ids": list(source_ids), "target_ids": list(target_ids), "reason": reason}
    proposal_id = f"foundry_{_sha256(_canonical_json(payload))[:24]}"
    result = {
        "proposal_id": proposal_id,
        "kind": kind,
        "project": project,
        "source_ids": list(source_ids),
        "target_ids": list(target_ids),
        "reason": reason,
        "score": round(min(max(float(score), 0.0), 1.0), 4),
        "recommended_action": action,
        "authority": "proposal",
        "auto_apply": False,
        "requires_confirmation": True,
        "undo_window_seconds": int(undo_window_seconds),
    }
    if extra:
        result.update(dict(extra))
    return result


def _semantic_type(record: Mapping[str, Any]) -> str:
    raw = f"{record.get('type', '')} {' '.join(str(item) for item in record.get('tags', []))}".casefold()
    for key in ("architecture", "bugfix", "feature", "refactor"):
        if key in raw:
            return key
    return "knowledge"


def _safe_text(value: Any, project: str, limit: int = MEMORY_FOUNDRY_MAX_TEXT) -> str:
    try:
        transformed = sanitize_llm_value(str(value or ""), source="memory-foundry", project=project, max_input_bytes=16_384, max_sanitized_bytes=16_384)
        return _clip(transformed.value, limit)
    except ValueError:
        return ""


def _bounded_strings(values: Any, limit: int, max_items: int, *, project: str = "blackholememory") -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    for value in values:
        item = _safe_text(value, project, limit)
        if item and item not in result:
            result.append(item)
        if len(result) >= max_items:
            break
    return result


def _bounded_score(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if number != number:
        number = default
    return round(min(max(number, 0.0), 1.0), 4)


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "MEMORY_FOUNDRY_MAX_PROPOSALS",
    "MEMORY_FOUNDRY_MAX_RECORDS",
    "MEMORY_FOUNDRY_SCHEMA_VERSION",
    "MemoryFoundryError",
    "build_memory_foundry_preview",
    "verify_memory_foundry_digest",
]
