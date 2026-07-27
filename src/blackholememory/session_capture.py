"""Deterministic session capture and progressive-disclosure previews.

WI-05 composes existing BHM authorities without creating another store. Raw
observation payloads remain in the observation journal; the returned packet
contains bounded event metadata, durable-memory summaries, proposals and
explicit provenance only.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .llm_safety import sanitize_llm_value


SESSION_CAPTURE_SCHEMA_VERSION = "bhm.session-capture.v1"
SESSION_CAPTURE_MAX_EVENTS = 128
SESSION_CAPTURE_MAX_MEMORIES = 96
SESSION_CAPTURE_MAX_TEXT = 640
SESSION_CAPTURE_MAX_ITEMS = 64
DISCLOSURE_LEVELS = ("brief", "standard", "deep", "audit")


class SessionCaptureError(ValueError):
    """Raised when a session capture request exceeds deterministic bounds."""


def build_session_capture_preview(
    observations: Sequence[Mapping[str, Any]],
    *,
    session_records: Sequence[Mapping[str, Any]] = (),
    memories: Sequence[Mapping[str, Any]] = (),
    project: str = "",
    session_id: str = "",
    disclosure: str = "standard",
    token_budget: int = 1_200,
    max_items: int = 32,
    stale_days: int = 90,
    undo_window_seconds: int = 900,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a bounded, read-only session packet.

    The function is intentionally pure. It may receive raw observations from
    ``ObservationStore`` but only emits identifiers, hashes, event dimensions
    and redacted metadata. ``now`` is injectable for deterministic benchmarks.
    """

    normalized_project = _clip(project, 120)
    normalized_session = _clip(session_id, 160)
    level = str(disclosure or "standard").strip().casefold()
    if level not in DISCLOSURE_LEVELS:
        raise SessionCaptureError(f"disclosure must be one of {', '.join(DISCLOSURE_LEVELS)}")
    if not normalized_project:
        raise SessionCaptureError("project is required")
    if not 64 <= int(token_budget) <= 16_384:
        raise SessionCaptureError("token_budget must be between 64 and 16384")
    if not 1 <= int(max_items) <= SESSION_CAPTURE_MAX_ITEMS:
        raise SessionCaptureError("max_items must be between 1 and 64")
    if not 1 <= int(stale_days) <= 3_650:
        raise SessionCaptureError("stale_days must be between 1 and 3650")
    if not 1 <= int(undo_window_seconds) <= 604_800:
        raise SessionCaptureError("undo_window_seconds must be between 1 and 604800")

    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    event_result = _normalize_events(
        observations,
        project=normalized_project,
        session_id=normalized_session,
        stale_days=int(stale_days),
        now=clock,
    )
    session_result = _normalize_session_records(
        session_records,
        project=normalized_project,
        session_id=normalized_session,
    )
    memory_result = _normalize_memories(
        memories,
        project=normalized_project,
        stale_days=int(stale_days),
        now=clock,
    )

    events = event_result["items"][: min(int(max_items), SESSION_CAPTURE_MAX_EVENTS)]
    memory_items = memory_result["items"][: min(int(max_items), SESSION_CAPTURE_MAX_MEMORIES)]
    session_items = session_result[: min(8, int(max_items))]
    crystals = _fact_crystals(memory_items, normalized_project)
    super_crystal = _super_crystal(crystals, normalized_project)
    working_summary = _working_summary(events, memory_items, session_items, normalized_project)
    provenance = _provenance(events, memory_items, session_items, normalized_project, normalized_session)
    forget_preview = _forget_preview(
        events,
        memory_items,
        project=normalized_project,
        session_id=normalized_session,
        undo_window_seconds=int(undo_window_seconds),
    )

    full = {
        "project": normalized_project,
        "session_id": normalized_session or None,
        "disclosure": level,
        "events": events,
        "session_records": session_items,
        "memories": memory_items,
        "working_summary": working_summary,
        "fact_crystals": crystals,
        "super_crystal": super_crystal,
        "diagnostics": {
            "duplicate_event_ids": event_result["duplicate_event_ids"],
            "duplicate_memory_groups": memory_result["duplicate_memory_groups"],
            "stale_event_count": event_result["stale_count"],
            "stale_memory_count": memory_result["stale_count"],
            "excluded_cross_project_count": event_result["excluded_cross_project"]
            + memory_result["excluded_cross_project"]
            + session_result_excluded_count(session_records, normalized_project),
            "raw_payload_returned": False,
            "transcript_authority": "observation-journal-only",
        },
        "provenance": provenance,
        "forget_preview": forget_preview,
    }
    projected = _project_disclosure(full, level)
    projected, budget = _fit_budget(projected, token_budget=int(token_budget), max_items=int(max_items))
    core = {
        "project": normalized_project,
        "session_id": normalized_session or None,
        "disclosure": level,
        "packet": projected,
        "budget": budget,
        "schema_version": SESSION_CAPTURE_SCHEMA_VERSION,
    }
    response_digest = _sha256(_canonical_json(core))
    return {
        "schema_version": SESSION_CAPTURE_SCHEMA_VERSION,
        "response_digest": response_digest,
        "project": normalized_project,
        "session_id": normalized_session or None,
        "disclosure": level,
        "packet": projected,
        "budget": budget,
        "counts": {
            "events": len(projected.get("events") or []),
            "session_records": len(projected.get("session_records") or []),
            "memories": len(projected.get("memories") or []),
            "fact_crystals": len(projected.get("fact_crystals") or []),
        },
        "execution": {
            "preview_only": True,
            "writes_sqlite": False,
            "writes_mem0": False,
            "writes_qdrant": False,
            "model_started": False,
            "auto_apply": False,
            "raw_payload_returned": False,
            "llm_authority": "proposal-only",
        },
    }


def verify_session_capture_digest(preview: Mapping[str, Any]) -> bool:
    expected = str(preview.get("response_digest") or "")
    if not expected:
        return False
    core = {
        "project": preview.get("project"),
        "session_id": preview.get("session_id"),
        "disclosure": preview.get("disclosure"),
        "packet": preview.get("packet"),
        "budget": preview.get("budget"),
        "schema_version": preview.get("schema_version"),
    }
    return expected == _sha256(_canonical_json(core))


def _normalize_events(
    observations: Sequence[Mapping[str, Any]],
    *,
    project: str,
    session_id: str,
    stale_days: int,
    now: datetime,
) -> dict[str, Any]:
    seen: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    duplicate_ids: list[str] = []
    excluded_cross_project = 0
    stale_count = 0
    for raw in list(observations)[: SESSION_CAPTURE_MAX_EVENTS * 2]:
        item = dict(raw)
        if str(item.get("project") or "") != project:
            excluded_cross_project += 1
            continue
        raw_session = str(item.get("sessionId") or item.get("session_id") or "")
        if session_id and raw_session and raw_session != session_id:
            continue
        event_id = _clip(item.get("eventId") or item.get("id"), 180)
        if not event_id:
            continue
        event_hash = _clip(item.get("recordSha256") or item.get("record_sha256"), 64)
        if not event_hash:
            event_hash = _sha256(_canonical_json({"id": event_id, "record": item}))
        if event_id in seen:
            if seen[event_id] != event_hash or event_id not in duplicate_ids:
                duplicate_ids.append(event_id)
            continue
        seen[event_id] = event_hash
        timestamp = _clip(item.get("timestamp") or item.get("occurredAt") or item.get("occurred_at"), 64)
        stale = _is_stale(timestamp, stale_days, now)
        stale_count += int(stale)
        payload = item.get("data") if isinstance(item.get("data"), Mapping) else {}
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        items.append(
            {
                "event_id": event_id,
                "session_id": raw_session or None,
                "correlation_id": _clip(item.get("correlationId") or item.get("correlation_id"), 180) or None,
                "parent_event_id": _clip(item.get("parentEventId") or item.get("parent_event_id"), 180) or None,
                "hook_type": _clip(item.get("hookType") or item.get("hook_type"), 80) or "observe",
                "source": _clip(item.get("source"), 80) or "hook",
                "occurred_at": timestamp or None,
                "payload_state": _clip(item.get("payloadState") or item.get("payload_state"), 40) or "sanitized",
                "sensitivity": _clip(item.get("sensitivity"), 40) or "internal",
                "payload_present": bool(payload),
                "payload_keys": sorted(_bounded_strings(list(payload.keys()), 80, 16)),
                "metadata_keys": sorted(_bounded_strings(list(metadata.keys()), 80, 16)),
                "record_sha256": event_hash,
                "stale": stale,
                "source_ref": {"kind": "observation", "id": event_id, "record_sha256": event_hash},
            }
        )
    items.sort(key=lambda value: (str(value.get("occurred_at") or ""), value["event_id"]), reverse=True)
    return {
        "items": items[:SESSION_CAPTURE_MAX_EVENTS],
        "duplicate_event_ids": sorted(duplicate_ids),
        "stale_count": stale_count,
        "excluded_cross_project": excluded_cross_project,
    }


def _normalize_session_records(
    records: Sequence[Mapping[str, Any]],
    *,
    project: str,
    session_id: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in list(records)[:32]:
        item = dict(raw)
        if str(item.get("project") or "") != project:
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        record_session = str(item.get("session_id") or item.get("sessionId") or metadata.get("session_id") or "")
        if session_id and record_session and record_session != session_id:
            continue
        if session_id and record_session == "" and str(item.get("id") or "") != session_id:
            # Session records created by the existing endpoint are project-scoped
            # artifacts. Keep them only when the caller did not request a strict
            # session id, avoiding accidental cross-session joins.
            continue
        record_id = _clip(item.get("id") or item.get("session_record_id"), 180)
        if not record_id:
            continue
        result.append(
            {
                "session_record_id": record_id,
                "title": _safe_text(item.get("title"), project, 180),
                "done": _safe_text(item.get("done"), project, 320),
                "next": _safe_text(item.get("next"), project, 320),
                "checks": _safe_text(item.get("checks"), project, 320),
                "risks": _safe_text(item.get("risks"), project, 320),
                "decisions": _safe_text(item.get("decisions"), project, 320),
                "updated_at": _clip(item.get("updated_at") or item.get("created_at"), 64) or None,
                "memory_id": _clip(item.get("memory_id"), 180) or None,
                "source_ref": {"kind": "session-record", "id": record_id},
            }
        )
    return sorted(result, key=lambda value: (str(value.get("updated_at") or ""), value["session_record_id"]), reverse=True)


def session_result_excluded_count(records: Sequence[Mapping[str, Any]], project: str) -> int:
    return sum(1 for item in records if str(item.get("project") or "") != project)


def _normalize_memories(
    memories: Sequence[Mapping[str, Any]],
    *,
    project: str,
    stale_days: int,
    now: datetime,
) -> dict[str, Any]:
    result: list[dict[str, Any]] = []
    content_groups: dict[str, list[str]] = {}
    excluded_cross_project = 0
    stale_count = 0
    for raw in list(memories)[: SESSION_CAPTURE_MAX_MEMORIES * 2]:
        item = dict(raw)
        if str(item.get("project") or "") != project:
            excluded_cross_project += 1
            continue
        memory_id = _clip(item.get("source_id") or item.get("id"), 180)
        if not memory_id:
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        content = _safe_text(item.get("content"), project, SESSION_CAPTURE_MAX_TEXT)
        content_digest = _sha256(content) if content else _sha256(memory_id)
        content_groups.setdefault(content_digest, []).append(memory_id)
        updated_at = _clip(item.get("updated_at") or item.get("created_at"), 64)
        stale = _is_stale(updated_at, stale_days, now)
        stale_count += int(stale)
        result.append(
            {
                "memory_id": memory_id,
                "memory_type": _clip(item.get("memory_type") or item.get("type"), 80) or "knowledge",
                "title": _safe_text(metadata.get("raw_title") or item.get("title") or (content.splitlines()[0] if content else ""), project, 180),
                "content_excerpt": content,
                "tags": _bounded_strings(item.get("tags") or item.get("concepts") or metadata.get("tags"), 80, 12),
                "files": _bounded_strings(metadata.get("files") or item.get("files"), 240, 12),
                "updated_at": updated_at or None,
                "created_at": _clip(item.get("created_at"), 64) or None,
                "confidence": _bounded_score(metadata.get("confidence"), default=0.5),
                "stale": stale,
                "content_sha256": content_digest,
                "source_ref": {"kind": "memory", "id": memory_id, "content_sha256": content_digest},
            }
        )
    result.sort(key=lambda value: (str(value.get("updated_at") or ""), value["memory_id"]), reverse=True)
    duplicate_groups = [sorted(ids) for ids in content_groups.values() if len(ids) > 1]
    return {
        "items": result[:SESSION_CAPTURE_MAX_MEMORIES],
        "duplicate_memory_groups": sorted(duplicate_groups),
        "stale_count": stale_count,
        "excluded_cross_project": excluded_cross_project,
    }


def _fact_crystals(memories: Sequence[Mapping[str, Any]], project: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for item in memories:
        key = str(item.get("memory_type") or "knowledge")
        tags = list(item.get("tags") or [])
        if tags:
            key = f"{key}:{tags[0]}"
        groups.setdefault(key, []).append(item)
    crystals: list[dict[str, Any]] = []
    for semantic_key, items in sorted(groups.items()):
        ids = sorted(str(item["memory_id"]) for item in items)[:12]
        payload = {"project": project, "semantic_key": semantic_key, "memory_ids": ids}
        crystals.append(
            {
                "crystal_id": f"fact_{_sha256(_canonical_json(payload))[:24]}",
                "semantic_key": semantic_key,
                "core_insight": _clip(str(items[0].get("title") or semantic_key), 240),
                "memory_ids": ids,
                "evidence_count": len(items),
                "authority": "proposal",
                "requires_confirmation": True,
                "source_refs": [dict(item["source_ref"]) for item in items[:12]],
            }
        )
    return crystals[:24]


def _super_crystal(crystals: Sequence[Mapping[str, Any]], project: str) -> dict[str, Any] | None:
    if not crystals:
        return None
    payload = {"project": project, "crystal_ids": [str(item["crystal_id"]) for item in crystals]}
    return {
        "crystal_id": f"super_{_sha256(_canonical_json(payload))[:24]}",
        "core_insight": f"{len(crystals)} reviewed-only fact crystal proposals for {project}",
        "fact_crystal_ids": list(payload["crystal_ids"]),
        "authority": "proposal",
        "requires_confirmation": True,
    }


def _working_summary(
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    session_records: Sequence[Mapping[str, Any]],
    project: str,
) -> dict[str, Any]:
    hook_counts = Counter(str(item.get("hook_type") or "observe") for item in events)
    tags = Counter(str(tag) for item in memories for tag in item.get("tags") or [])
    return {
        "project": project,
        "event_count": len(events),
        "memory_count": len(memories),
        "session_record_count": len(session_records),
        "dominant_hook_types": [name for name, _count in hook_counts.most_common(6)],
        "top_memory_tags": [name for name, _count in tags.most_common(8)],
        "next_steps": [_clip(item.get("next"), 240) for item in session_records if item.get("next")][:4],
        "summary_authority": "deterministic-composition",
    }


def _provenance(
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    project: str,
    session_id: str,
) -> dict[str, Any]:
    return {
        "project": project,
        "session_id": session_id or None,
        "authority": "sqlite-authoritative",
        "observation_event_ids": [str(item["event_id"]) for item in events],
        "memory_ids": [str(item["memory_id"]) for item in memories],
        "session_record_ids": [str(item["session_record_id"]) for item in sessions],
        "source_kinds": ["observation", "session-record", "memory"],
    }


def _forget_preview(
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    *,
    project: str,
    session_id: str,
    undo_window_seconds: int,
) -> dict[str, Any]:
    event_ids = sorted(str(item["event_id"]) for item in events)
    memory_ids = sorted(str(item["memory_id"]) for item in memories)
    payload = {
        "project": project,
        "session_id": session_id or None,
        "event_ids": event_ids,
        "memory_ids": memory_ids,
        "undo_window_seconds": int(undo_window_seconds),
    }
    digest = _sha256(_canonical_json(payload))
    return {
        "preview_digest": digest,
        "event_ids": event_ids,
        "memory_ids": memory_ids,
        "undo_window_seconds": int(undo_window_seconds),
        "undo_token_digest": _sha256(f"{digest}:{int(undo_window_seconds)}"),
        "preview_only": True,
        "requires_confirmation": True,
        "auto_apply": False,
        "mutation_authority": "existing tombstone/retention paths",
    }


def _project_disclosure(full: Mapping[str, Any], level: str) -> dict[str, Any]:
    events = list(full.get("events") or [])
    memories = list(full.get("memories") or [])
    sessions = list(full.get("session_records") or [])
    if level == "brief":
        events = [_brief_event(item) for item in events]
        memories = [_brief_memory(item) for item in memories[:8]]
        sessions = [_brief_session(item) for item in sessions[:4]]
        return {"events": events, "memories": memories, "session_records": sessions, "working_summary": full["working_summary"]}
    result = {
        "events": events,
        "memories": memories,
        "session_records": sessions,
        "working_summary": full["working_summary"],
        "provenance": full["provenance"],
    }
    if level in {"deep", "audit"}:
        result["fact_crystals"] = full["fact_crystals"]
        result["super_crystal"] = full["super_crystal"]
        result["diagnostics"] = full["diagnostics"]
    if level == "audit":
        result["forget_preview"] = full["forget_preview"]
    return result


def _fit_budget(packet: dict[str, Any], *, token_budget: int, max_items: int) -> tuple[dict[str, Any], dict[str, Any]]:
    for key in ("events", "memories", "session_records"):
        values = list(packet.get(key) or [])
        packet[key] = values[:max_items]
    estimated = _estimate_tokens(packet)
    truncated = False
    while estimated > token_budget and any(packet.get(key) for key in ("events", "memories", "session_records")):
        key = max((name for name in ("events", "memories", "session_records") if packet.get(name)), key=lambda name: len(packet[name]))
        packet[key].pop()
        truncated = True
        estimated = _estimate_tokens(packet)
    omissions = []
    if truncated:
        omissions.append("items_truncated_to_token_budget")
    if "fact_crystals" in packet and estimated > token_budget:
        packet["fact_crystals"] = []
        packet["super_crystal"] = None
        omissions.append("crystals_omitted_to_token_budget")
        estimated = _estimate_tokens(packet)
    return packet, {"token_budget": token_budget, "estimated_tokens": estimated, "truncated": truncated, "omissions": omissions}


def _brief_event(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ("event_id", "hook_type", "source", "occurred_at", "stale", "source_ref")}


def _brief_memory(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ("memory_id", "memory_type", "title", "updated_at", "stale", "source_ref")}


def _brief_session(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ("session_record_id", "title", "updated_at", "source_ref")}


def _safe_text(value: Any, project: str, limit: int) -> str:
    try:
        result = sanitize_llm_value(
            str(value or ""),
            source="session-capture",
            project=project,
            max_input_bytes=16_384,
            max_sanitized_bytes=16_384,
        )
        return _clip(result.value, limit)
    except ValueError:
        return ""


def _bounded_strings(values: Any, limit: int, max_items: int) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    for value in values:
        item = _clip(value, limit)
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


def _is_stale(value: str, stale_days: int, now: datetime) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age_days = max((now - parsed.astimezone(timezone.utc)).total_seconds() / 86_400, 0.0)
    return age_days >= stale_days


def _estimate_tokens(value: Any) -> int:
    return max(1, (len(_canonical_json(value).encode("utf-8")) + 3) // 4)


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "DISCLOSURE_LEVELS",
    "SESSION_CAPTURE_MAX_EVENTS",
    "SESSION_CAPTURE_SCHEMA_VERSION",
    "SessionCaptureError",
    "build_session_capture_preview",
    "verify_session_capture_digest",
]
