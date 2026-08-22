"""Governed bitemporal contract for WL-300.2.

The contract is additive and dark by default.  SQLite remains authoritative;
Qdrant receives only the bounded projection fields returned here.  Legacy
rows may have unknown observation time and remain readable through ordinary
current retrieval, but historical queries fail closed unless the operator
explicitly opts into unknown temporal provenance.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .filesystem_boundaries import assert_safe_path


FEATURE_FLAG = "BHM_TEMPORAL_CONTRACT_ENABLED"
PROJECTION_READY_FLAG = "BHM_TEMPORAL_PROJECTION_READY"
CAPABILITY_KEY = "temporal_memory_contract_version"
CAPABILITY_VERSION = "1"
TEMPORAL_SCHEMA_VERSION = "bhm.temporal-memory.v1"

TEMPORAL_COLUMNS = frozenset(
    {
        "observed_at",
        "observed_at_source",
        "valid_from",
        "valid_to",
        "open_interval",
        "supersedes_revision_id",
        "source_episode_id",
        "source_uri",
        "source_digest",
    }
)
TEMPORAL_INDEXES = frozenset(
    {
        "idx_memories_project_validity_time",
        "idx_memories_project_observed_time",
    }
)
TEMPORAL_TABLES = frozenset({"memory_temporal_conflicts"})

OBSERVED_AT_SOURCES = frozenset(
    {"explicit", "transaction-clock", "imported", "legacy-unknown"}
)
CONFLICT_TYPES = frozenset({"contradiction", "supersession", "source-dispute"})

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class TemporalContractUnavailable(RuntimeError):
    """Raised when temporal intent is used outside its migration gate."""


class TemporalValidationError(ValueError):
    """Raised when temporal fields cannot be represented safely."""


def _runtime_flag(name: str) -> str:
    """Read an explicit process flag, then the local BHM runtime config.

    The authoritative launcher intentionally does not import a user's entire
    environment file into its process.  Temporal activation still needs a
    durable local operator setting, so only the requested non-secret flag is
    read as a fallback.  A process value always wins for tests and rollback.
    """

    process_value = os.getenv(name)
    if process_value is not None and process_value.strip():
        return process_value
    config_path = Path.home() / ".bhm" / ".env"
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key.strip() == name:
            return value.split("#", 1)[0].strip()
    return ""


def temporal_contract_enabled() -> bool:
    raw = _runtime_flag(FEATURE_FLAG).strip().casefold()
    if raw in _FALSE_VALUES:
        return False
    if raw in _TRUE_VALUES:
        return True
    return False


def temporal_projection_ready() -> bool:
    raw = _runtime_flag(PROJECTION_READY_FLAG).strip().casefold()
    return raw in _TRUE_VALUES


def normalize_temporal_timestamp(value: Any, field_name: str, *, allow_none: bool = True) -> str | None:
    if value is None or str(value).strip() == "":
        if allow_none:
            return None
        raise TemporalValidationError(f"{field_name} is required")
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TemporalValidationError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TemporalValidationError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def temporal_timestamp(value: str | None) -> datetime | None:
    normalized = normalize_temporal_timestamp(value, "timestamp")
    if normalized is None:
        return None
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def validate_temporal_interval(
    valid_from: Any,
    valid_to: Any,
    open_interval: Any,
) -> tuple[str | None, str | None, bool]:
    start = normalize_temporal_timestamp(valid_from, "valid_from")
    end = normalize_temporal_timestamp(valid_to, "valid_to")
    is_open = bool(open_interval)
    if is_open and end is not None:
        raise TemporalValidationError("open_interval=true requires valid_to to be null")
    if not is_open and end is None:
        raise TemporalValidationError("open_interval=false requires valid_to")
    if start is not None and end is not None and temporal_timestamp(start) >= temporal_timestamp(end):
        raise TemporalValidationError("valid_from must be earlier than valid_to")
    return start, end, is_open


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def temporal_fields_from_record(record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    def pick(name: str, *aliases: str) -> Any:
        for key in (name, *aliases):
            if key in record and record.get(key) is not None:
                return record.get(key)
            if key in metadata and metadata.get(key) is not None:
                return metadata.get(key)
        return None

    valid_to = pick("valid_to", "temporal_valid_to")
    raw_open = pick("open_interval", "temporal_open_interval")
    open_interval = True if raw_open is None else bool(raw_open)
    return {
        "observed_at": pick("observed_at", "temporal_observed_at"),
        "observed_at_source": pick("observed_at_source", "temporal_observed_at_source") or "legacy-unknown",
        "valid_from": pick("valid_from", "temporal_valid_from"),
        "valid_to": valid_to,
        "open_interval": open_interval,
        "supersedes_revision_id": pick("supersedes_revision_id", "supersedes_revision", "temporal_supersedes_revision_id"),
        "source_episode_id": pick("source_episode_id", "temporal_source_episode_id"),
        "source_uri": pick("source_uri", "temporal_source_uri"),
        "source_digest": pick("source_digest", "temporal_source_digest"),
    }


def normalize_temporal_fields(record: Mapping[str, Any], *, require_observed: bool = False) -> dict[str, Any]:
    fields = temporal_fields_from_record(record)
    observed_at = normalize_temporal_timestamp(fields["observed_at"], "observed_at", allow_none=not require_observed)
    observed_source = _text(fields["observed_at_source"]) or ("transaction-clock" if observed_at else "legacy-unknown")
    if observed_source not in OBSERVED_AT_SOURCES:
        raise TemporalValidationError(f"observed_at_source must be one of {sorted(OBSERVED_AT_SOURCES)}")
    if require_observed and observed_at is None:
        raise TemporalValidationError("observed_at is required for temporal writes")
    valid_from, valid_to, open_interval = validate_temporal_interval(
        fields["valid_from"], fields["valid_to"], fields["open_interval"]
    )
    source_digest = _text(fields["source_digest"])
    if source_digest is not None and (len(source_digest) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in source_digest)):
        raise TemporalValidationError("source_digest must be a SHA-256 hex digest")
    return {
        "observed_at": observed_at,
        "observed_at_source": observed_source,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "open_interval": open_interval,
        "supersedes_revision_id": _text(fields["supersedes_revision_id"]),
        "source_episode_id": _text(fields["source_episode_id"]),
        "source_uri": _text(fields["source_uri"]),
        "source_digest": source_digest.lower() if source_digest else None,
    }


def temporal_intent_requested(**fields: Any) -> bool:
    return any(value is not None for value in fields.values())


def require_temporal_contract(*, capability_available: bool) -> None:
    if not temporal_contract_enabled():
        raise TemporalContractUnavailable("temporal_memory_contract_disabled")
    if not capability_available:
        raise TemporalContractUnavailable("temporal_memory_contract_migration_required")


def temporal_capability_available(database: str | Path) -> bool:
    path = assert_safe_path(database).resolve()
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2.0) as connection:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memories)").fetchall()}
            indexes = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            marker = connection.execute(
                "SELECT value FROM memory_store_meta WHERE key = ?", (CAPABILITY_KEY,)
            ).fetchone()
    except (OSError, sqlite3.Error):
        return False
    return (
        TEMPORAL_COLUMNS.issubset(columns)
        and TEMPORAL_INDEXES.issubset(indexes)
        and TEMPORAL_TABLES.issubset(tables)
        and marker is not None
        and str(marker[0]) == CAPABILITY_VERSION
    )


def temporal_projection_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    fields = normalize_temporal_fields(record)
    return {
        "observed_at": fields["observed_at"],
        "observed_at_source": fields["observed_at_source"],
        "valid_from": fields["valid_from"],
        "valid_to": fields["valid_to"],
        "open_interval": fields["open_interval"],
        "supersedes_revision_id": fields["supersedes_revision_id"],
        "source_episode_id": fields["source_episode_id"],
        "source_uri": fields["source_uri"],
        "source_digest": fields["source_digest"],
    }


def temporal_projection_digest(record: Mapping[str, Any]) -> str:
    payload = temporal_projection_fields(record)
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def temporal_matches(
    record: Mapping[str, Any],
    *,
    as_of: Any = None,
    valid_from: Any = None,
    valid_to: Any = None,
    include_temporal_unknown: bool = False,
) -> bool:
    if as_of is None and valid_from is None and valid_to is None:
        return True
    fields = normalize_temporal_fields(record)
    query_as_of = normalize_temporal_timestamp(as_of, "as_of")
    query_from = normalize_temporal_timestamp(valid_from, "query_valid_from")
    query_to = normalize_temporal_timestamp(valid_to, "query_valid_to")
    if query_from is not None and query_to is not None and temporal_timestamp(query_from) >= temporal_timestamp(query_to):
        raise TemporalValidationError("query_valid_from must be earlier than query_valid_to")
    observed = temporal_timestamp(fields["observed_at"])
    if observed is None and not include_temporal_unknown:
        return False
    if query_as_of is not None:
        point = temporal_timestamp(query_as_of)
        if observed is not None and observed > point:
            return False
        start = temporal_timestamp(fields["valid_from"])
        end = temporal_timestamp(fields["valid_to"])
        if start is not None and point < start:
            return False
        if end is not None and point >= end:
            return False
    if query_from is not None or query_to is not None:
        record_start = temporal_timestamp(fields["valid_from"])
        record_end = temporal_timestamp(fields["valid_to"])
        # Half-open intervals: [start, end).  Null bounds are unbounded.
        if query_to is not None and record_start is not None and record_start >= temporal_timestamp(query_to):
            return False
        if query_from is not None and record_end is not None and record_end <= temporal_timestamp(query_from):
            return False
    return True


class TemporalConflictReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conflict_id: str
    project: str
    memory_id: str
    conflict_type: str
    reason: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    actor: str
    created_at: str
    source_episode_id: str | None = None
    source_uri: str | None = None
    source_digest: str | None = None
    resolution: str = "open"

    @field_validator("conflict_id", "project", "memory_id", "reason", "actor", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("temporal conflict identity/text must not be empty")
        return text

    @field_validator("conflict_type", mode="before")
    @classmethod
    def _conflict_type(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text not in CONFLICT_TYPES:
            raise ValueError(f"conflict_type must be one of {sorted(CONFLICT_TYPES)}")
        return text

    @field_validator("created_at", mode="before")
    @classmethod
    def _created_at(cls, value: Any) -> str:
        return normalize_temporal_timestamp(value, "created_at", allow_none=False) or ""

    @field_validator("source_digest", mode="before")
    @classmethod
    def _source_digest(cls, value: Any) -> str | None:
        text = _text(value)
        if text is not None and (len(text) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in text)):
            raise ValueError("source_digest must be a SHA-256 hex digest")
        return text.lower() if text else None


__all__ = [
    "CAPABILITY_KEY",
    "CAPABILITY_VERSION",
    "CONFLICT_TYPES",
    "FEATURE_FLAG",
    "OBSERVED_AT_SOURCES",
    "PROJECTION_READY_FLAG",
    "TEMPORAL_COLUMNS",
    "TEMPORAL_INDEXES",
    "TEMPORAL_SCHEMA_VERSION",
    "TEMPORAL_TABLES",
    "TemporalConflictReceipt",
    "TemporalContractUnavailable",
    "TemporalValidationError",
    "normalize_temporal_fields",
    "normalize_temporal_timestamp",
    "require_temporal_contract",
    "temporal_capability_available",
    "temporal_contract_enabled",
    "temporal_fields_from_record",
    "temporal_intent_requested",
    "temporal_matches",
    "temporal_projection_digest",
    "temporal_projection_fields",
    "temporal_projection_ready",
    "validate_temporal_interval",
]
