"""Preview-only lifecycle suggestions with deterministic digests and undo policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_UNDO_WINDOW_SECONDS = 900
MAX_SUGGESTIONS = 64


def build_lifecycle_suggestions(
    queue_items: Sequence[Mapping[str, Any]],
    *,
    undo_window_seconds: int = DEFAULT_UNDO_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Convert review/triage candidates into non-mutating action previews."""

    window = max(min(int(undo_window_seconds), 604800), 1)
    suggestions: list[dict[str, Any]] = []
    for item in list(queue_items)[:MAX_SUGGESTIONS]:
        if not isinstance(item, Mapping):
            continue
        action = _action_for_kind(str(item.get("kind") or ""), item.get("reasons"))
        if action is None:
            continue
        memory_ids = _bounded_ids(item.get("memory_ids"))
        if not memory_ids:
            continue
        reason_codes = _bounded_reasons(item.get("reasons"))
        plan = {
            "schema_version": SCHEMA_VERSION,
            "queue_id": str(item.get("queue_id") or "")[:128],
            "kind": str(item.get("kind") or "")[:64],
            "action": action,
            "memory_ids": memory_ids,
            "reason_codes": reason_codes,
            "score": _bounded_score(item.get("score")),
            "undo_window_seconds": window,
        }
        digest = _plan_digest(plan)
        suggestions.append(
            {
                **plan,
                "preview_digest": digest,
                "status": str(item.get("status") or "open")[:32],
                "requires_confirmation": True,
                "auto_apply": False,
                "undo": {"available": True, "window_seconds": window, "requires_digest": True},
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "mutation": False,
        "auto_apply": False,
        "suggestions": suggestions,
        "count": len(suggestions),
    }


def verify_preview_digest(suggestion: Mapping[str, Any], expected_digest: str) -> bool:
    """Verify a preview digest without applying any lifecycle action."""

    plan = {
        "schema_version": suggestion.get("schema_version", SCHEMA_VERSION),
        "queue_id": str(suggestion.get("queue_id") or "")[:128],
        "kind": str(suggestion.get("kind") or "")[:64],
        "action": str(suggestion.get("action") or "")[:64],
        "memory_ids": _bounded_ids(suggestion.get("memory_ids")),
        "reason_codes": _bounded_reasons(suggestion.get("reason_codes")),
        "score": _bounded_score(suggestion.get("score")),
        "undo_window_seconds": max(min(int(suggestion.get("undo_window_seconds") or DEFAULT_UNDO_WINDOW_SECONDS), 604800), 1),
    }
    return _plan_digest(plan) == str(expected_digest or "").strip().lower()


def _action_for_kind(kind: str, reasons: Any) -> str | None:
    normalized = kind.strip().casefold()
    if normalized == "duplicate":
        return "merge_preview"
    if normalized in {"conflict", "contradiction"}:
        return "contradiction_review"
    if normalized == "quality":
        reason_set = set(_bounded_reasons(reasons))
        if reason_set & {"low_confidence", "missing_source_refs", "possible_secret"}:
            return "archive_preview"
    return None


def _plan_digest(plan: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(plan), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bounded_ids(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return sorted({str(item).strip()[:128] for item in value if str(item).strip()})[:8]


def _bounded_reasons(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        values = []
    return sorted({str(item).strip()[:64] for item in values if str(item).strip()})[:8]


def _bounded_score(value: Any) -> float:
    try:
        return round(min(max(float(value), 0.0), 1.0), 6)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["build_lifecycle_suggestions", "verify_preview_digest"]
