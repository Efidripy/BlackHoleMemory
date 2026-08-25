"""Digest-bound reclassification of durable checkpoint/session history.

The operation never deletes, rewrites content, or changes a memory revision.
It is deliberately separate from startup: an operator first produces a
read-only plan, then applies that exact plan while the authority writer is
offline.  Each changed aggregate uses the ordinary SQLite save path, which
adds an outbox event for the existing Qdrant projector.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .domain import Memory
from .filesystem_boundaries import assert_safe_path
from .memory_contracts import EVENT_ROLE_SCHEMA_VERSION, MemoryClass, MemoryClassSource, MemoryEventRole
from .memory_repository import MemoryRevisionConflict, SQLiteMemoryRepository
from .outbox import utc_now_iso


SCHEMA_VERSION = "bhm.historical-record-backfill.v1"
_TARGETS = (("checkpoint:", "checkpoint"), ("session-record:", "session-record"))


class HistoricalRecordBackfillError(RuntimeError):
    """Raised for a plan or authority precondition that must fail closed."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _artifact_kind(upsert_key: str) -> str | None:
    for prefix, kind in _TARGETS:
        if upsert_key.startswith(prefix):
            return kind
    return None


def _read_target_rows(database: Path) -> list[dict[str, str]]:
    uri = f"file:{database.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
        connection.row_factory = sqlite3.Row
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold()
        if quick_check != "ok":
            raise HistoricalRecordBackfillError(f"SQLite quick_check failed: {quick_check}")
        rows = connection.execute(
            """
            SELECT memory_id, project, current_revision_id, upsert_key,
                   memory_class, event_role,
                   (SELECT content_sha256 FROM memory_revisions AS r
                    WHERE r.revision_id = memories.current_revision_id) AS content_sha256
            FROM memories
            WHERE lifecycle != 'tombstoned'
              AND (upsert_key LIKE 'checkpoint:%' OR upsert_key LIKE 'session-record:%')
            ORDER BY memory_id
            """
        ).fetchall()
    targets: list[dict[str, str]] = []
    for row in rows:
        upsert_key = str(row["upsert_key"] or "")
        kind = _artifact_kind(upsert_key)
        if kind is None:
            continue
        # A fully classified record is already idempotently converged.
        if str(row["memory_class"]) == MemoryClass.EPISODIC.value and str(row["event_role"]) == MemoryEventRole.TRACE.value:
            continue
        content_sha256 = str(row["content_sha256"] or "")
        if len(content_sha256) != 64:
            raise HistoricalRecordBackfillError(f"target revision digest is missing: {row['memory_id']}")
        targets.append(
            {
                "memory_id": str(row["memory_id"]),
                "project": str(row["project"]),
                "revision_id": str(row["current_revision_id"]),
                "content_sha256": content_sha256,
                "upsert_key": upsert_key,
                "artifact_kind": kind,
            }
        )
    return targets


def build_historical_record_backfill_plan(
    database: str | Path,
    existing_backup: str | Path,
) -> dict[str, Any]:
    """Build a read-only exact plan for legacy checkpoint/session records."""

    database_path = assert_safe_path(database).resolve()
    backup_path = assert_safe_path(existing_backup).resolve()
    if not database_path.is_file():
        raise HistoricalRecordBackfillError(f"SQLite database is missing: {database_path}")
    if not backup_path.is_file():
        raise HistoricalRecordBackfillError(f"verified backup is missing: {backup_path}")
    targets = _read_target_rows(database_path)
    target_digest = hashlib.sha256(canonical_json(targets).encode("utf-8")).hexdigest()
    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "database_path": str(database_path),
        "backup": {"path": str(backup_path), "sha256": sha256_file(backup_path)},
        "target_snapshot_digest": target_digest,
        "targets": targets,
        "summary": {
            "target_count": len(targets),
            "checkpoint_count": sum(item["artifact_kind"] == "checkpoint" for item in targets),
            "session_record_count": sum(item["artifact_kind"] == "session-record" for item in targets),
            "mutation": "metadata-and-typed-classification-only",
            "content_or_revision_rewrite": False,
            "projection": "existing_sqlite_outbox_projector",
        },
        "execution": {
            "read_only": True,
            "sqlite_written": False,
            "qdrant_written": False,
            "requires_exact_plan_digest": True,
            "requires_verified_backup": True,
            "requires_offline_verified": True,
        },
    }
    plan["plan_digest"] = hashlib.sha256(canonical_json(plan).encode("utf-8")).hexdigest()
    return plan


def apply_historical_record_backfill(
    database: str | Path,
    existing_backup: str | Path,
    plan: Mapping[str, Any],
    *,
    expected_plan_digest: str,
    confirm_operator: bool = False,
    offline_verified: bool = False,
) -> dict[str, Any]:
    """Apply one revalidated plan through SQLite's normal outbox transaction."""

    if not confirm_operator:
        raise HistoricalRecordBackfillError("explicit operator confirmation is required")
    if not offline_verified:
        raise HistoricalRecordBackfillError("offline authority-writer verification is required")
    received = str(plan.get("plan_digest") or "")
    if received != expected_plan_digest:
        raise HistoricalRecordBackfillError("expected plan digest does not match supplied plan")
    current = build_historical_record_backfill_plan(database, existing_backup)
    if current["plan_digest"] != received:
        raise HistoricalRecordBackfillError("authoritative target set changed since plan")
    repository = SQLiteMemoryRepository(assert_safe_path(database).resolve())
    updates: list[Memory] = []
    expected_revisions: dict[str, str] = {}
    now = utc_now_iso()
    for target in current["targets"]:
        memory_id = str(target["memory_id"])
        project = str(target["project"])
        memory = repository.get_memory(memory_id, project=project)
        if memory is None:
            raise HistoricalRecordBackfillError(f"planned memory is absent: {memory_id}")
        if memory.current_revision.revision_id != target["revision_id"] or memory.current_revision.content_sha256 != target["content_sha256"]:
            raise HistoricalRecordBackfillError(f"planned memory revision drifted: {memory_id}")
        metadata = dict(memory.metadata)
        metadata["artifact_kind"] = str(target["artifact_kind"])
        metadata["historical_record_backfill"] = {
            "schema_version": SCHEMA_VERSION,
            "plan_digest": received,
            "artifact_kind": str(target["artifact_kind"]),
        }
        updates.append(
            memory.model_copy(
                update={
                    "memory_class": MemoryClass.EPISODIC,
                    "memory_class_source": MemoryClassSource.DETERMINISTIC_RULE,
                    "memory_class_confidence": 1.0,
                    "event_role": MemoryEventRole.TRACE,
                    "event_role_version": EVENT_ROLE_SCHEMA_VERSION,
                    "metadata": metadata,
                    "updated_at": now,
                }
            )
        )
        expected_revisions[memory_id] = memory.current_revision.revision_id
    try:
        results = repository.save_memories_atomic(updates, expected_revision_ids=expected_revisions)
    except MemoryRevisionConflict as exc:
        raise HistoricalRecordBackfillError("authoritative target set changed before apply") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "applied",
        "plan_digest": received,
        "target_count": len(results),
        "memory_ids": [result.memory.id for result in results],
        "outbox_event_ids": [result.outbox_event_id for result in results],
        "content_or_revision_rewrite": False,
        "projection": "existing_sqlite_outbox_projector",
    }


__all__ = [
    "HistoricalRecordBackfillError",
    "SCHEMA_VERSION",
    "apply_historical_record_backfill",
    "build_historical_record_backfill_plan",
]
