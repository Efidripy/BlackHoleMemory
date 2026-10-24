from __future__ import annotations

import json
import math
from datetime import datetime
from datetime import timezone
from typing import Any

from .config import settings
from .filesystem_boundaries import append_bytes_safely

DECAY_ARCHIVE_PATH = settings.runtime_dir / "archive" / "decayed_memory_vault.json"
DECAY_LAMBDA_PER_DAY = 0.05
DECAY_LAMBDA_BY_SEMANTIC_TYPE = {
    "architecture": 0.025,
    "knowledge": 0.03,
    "decision-log": 0.025,
    "requirement": 0.05,
    "fact": 0.04,
    "bugfix": 0.08,
    "feature": 0.07,
    "refactor": 0.06,
    "log": 0.12,
    "error": 0.12,
}
DECAY_LAMBDA_BY_MEMORY_TYPE = {
    "architecture": 0.025,
    "knowledge": 0.03,
    "knowledge-crystal": 0.025,
    "fact-crystal": 0.025,
    "pattern": 0.025,
    "workflow": 0.08,
    "observation": 0.12,
    "bug": 0.08,
    "bugfix": 0.08,
    "feature": 0.07,
    "refactor": 0.06,
    "log": 0.12,
    "error": 0.12,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_memory_timestamp(value: Any, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text) if text else None
        except ValueError:
            parsed = None
    if parsed is None:
        parsed = default or datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_importance_score(value: Any, default: int = 5) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = default
    return max(1, min(score, 10))


def normalize_access_count(value: Any, default: int = 1) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = default
    return max(count, 1)


def decay_lambda_for_payload(payload: dict[str, Any]) -> float:
    """Return a bounded retention rate using explicit taxonomy first."""

    nested_metadata = payload.get("metadata")
    metadata = nested_metadata if isinstance(nested_metadata, dict) else {}
    override = payload.get("decay_lambda_per_day") or metadata.get("decay_lambda_per_day")
    try:
        if override is not None:
            return max(0.001, min(float(override), 1.0))
    except (TypeError, ValueError):
        pass

    semantic_type = str(payload.get("semantic_type") or metadata.get("semantic_type") or "").strip().casefold()
    if semantic_type in DECAY_LAMBDA_BY_SEMANTIC_TYPE:
        return DECAY_LAMBDA_BY_SEMANTIC_TYPE[semantic_type]

    memory_type = str(
        payload.get("memory_type") or payload.get("type") or metadata.get("memory_type") or metadata.get("type") or ""
    ).strip().casefold()
    if memory_type in DECAY_LAMBDA_BY_MEMORY_TYPE:
        return DECAY_LAMBDA_BY_MEMORY_TYPE[memory_type]

    scope = str(payload.get("scope") or metadata.get("scope") or "").strip().casefold()
    if scope == "global":
        return 0.03
    return DECAY_LAMBDA_PER_DAY


def memory_decay_score(payload: dict[str, Any], raw_qdrant_score: float = 1.0, now: datetime | None = None) -> float:
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_dt = now_dt.astimezone(timezone.utc)

    last_accessed = _parse_memory_timestamp(
        payload.get("last_accessed_at") or payload.get("updated_at") or payload.get("created_at"),
        default=now_dt,
    )
    delta_days = max((now_dt - last_accessed).days, 0)
    retention = math.exp(-decay_lambda_for_payload(payload) * delta_days)
    importance = normalize_importance_score(payload.get("importance_score"))
    access_count = normalize_access_count(payload.get("access_count"))
    return float(raw_qdrant_score) * (importance / 5.0) * retention * (1.0 + 0.1 * math.log(access_count))


def initial_decay_metadata(metadata: dict[str, Any] | None = None, *, created_at: str | None = None) -> dict[str, Any]:
    payload = dict(metadata or {})
    now = created_at or utc_now_iso()
    payload["importance_score"] = normalize_importance_score(payload.get("importance_score"))
    payload["access_count"] = 1
    payload["last_accessed_at"] = now
    return payload


def ensure_decay_metadata(metadata: dict[str, Any] | None = None, *, fallback_at: str | None = None) -> dict[str, Any]:
    payload = dict(metadata or {})
    now = fallback_at or utc_now_iso()
    payload["importance_score"] = normalize_importance_score(payload.get("importance_score"))
    payload["access_count"] = normalize_access_count(payload.get("access_count"))
    payload["last_accessed_at"] = str(payload.get("last_accessed_at") or now)
    return payload


def _append_decayed_payload_archive(
    *,
    collection_name: str,
    point_id: Any,
    payload: dict[str, Any],
    score: float,
    threshold: float,
    archived_at: str,
) -> None:
    archive_record = {
        "archived_at": archived_at,
        "collection": collection_name,
        "point_id": str(point_id),
        "decay_score": score,
        "threshold": threshold,
        "payload": payload,
    }
    append_bytes_safely(
        DECAY_ARCHIVE_PATH,
        (json.dumps(archive_record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
    )
