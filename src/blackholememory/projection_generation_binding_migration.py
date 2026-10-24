"""Explicit, additive SQLite migration for projection-generation bindings.

The migration stores only routing provenance and immutable state transitions.
It never writes memories, outbox rows, Mem0 state, or Qdrant collections.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Mapping

from .filesystem_boundaries import assert_safe_path
from .memory_repository import (
    GOVERNED_CONSOLIDATION_SCHEMA_VERSION,
    PROJECTION_GENERATION_BINDING_SCHEMA_TABLES,
    PROJECTION_GENERATION_BINDING_SCHEMA_VERSION,
    MemoryRepositoryError,
)

MIGRATION_SCHEMA_VERSION = "bhm.projection-generation-binding-migration.v1"
_BASE_TABLES = frozenset(
    {"memory_store_meta", "memories", "memory_revisions", "memory_artifacts", "memory_links", "memory_outbox"}
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar_fingerprint(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    return {"size": int(path.stat().st_size), "sha256": _sha256_file(path)}


def _fingerprint(path: Path | str) -> dict[str, object]:
    target = assert_safe_path(Path(path).expanduser()).resolve()
    if not target.is_file():
        raise MemoryRepositoryError("SQLite database is missing")
    uri = f"file:{target.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    stat = target.stat()
    return {
        "path": str(target),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": _sha256_file(target),
        "wal": _sidecar_fingerprint(Path(f"{target}-wal")),
        "user_version": version,
        "tables": sorted(tables),
        "quick_check": quick_check,
        "foreign_key_errors": len(foreign_keys),
    }


def projection_generation_binding_migration_status(path: Path | str) -> dict[str, object]:
    target = Path(path).expanduser()
    assert_safe_path(target)
    if not target.is_file():
        return {"ready": False, "reason": "database_missing", "path": str(target)}
    try:
        fingerprint = _fingerprint(target)
    except (OSError, sqlite3.Error, MemoryRepositoryError) as exc:
        return {"ready": False, "reason": type(exc).__name__, "path": str(target)}
    tables = set(fingerprint["tables"])
    return {
        "ready": bool(
            fingerprint["user_version"] == PROJECTION_GENERATION_BINDING_SCHEMA_VERSION
            and PROJECTION_GENERATION_BINDING_SCHEMA_TABLES.issubset(tables)
            and fingerprint["quick_check"] == "ok"
            and not fingerprint["foreign_key_errors"]
        ),
        **fingerprint,
    }


def build_projection_generation_binding_migration_plan(
    database: Path | str,
    existing_backup: Path | str,
    *,
    as_of: str,
) -> dict[str, object]:
    """Build a digest-bound read-only plan for the additive v3-to-v4 migration."""

    if not str(as_of).strip():
        raise MemoryRepositoryError("as_of is required")
    target = assert_safe_path(Path(database).expanduser()).resolve()
    backup = assert_safe_path(Path(existing_backup).expanduser()).resolve()
    if target == backup or not backup.is_file():
        raise MemoryRepositoryError("a distinct existing SQLite backup is required")
    current = _fingerprint(target)
    if current["user_version"] != GOVERNED_CONSOLIDATION_SCHEMA_VERSION:
        raise MemoryRepositoryError("projection binding migration requires a verified v3 SQLite authority store")
    if not _BASE_TABLES.issubset(set(current["tables"])):
        raise MemoryRepositoryError("authoritative memory schema is incomplete")
    if current["quick_check"] != "ok" or current["foreign_key_errors"]:
        raise MemoryRepositoryError("authoritative SQLite integrity preflight failed")
    backup_fingerprint = _fingerprint(backup)
    if backup_fingerprint["quick_check"] != "ok" or backup_fingerprint["foreign_key_errors"]:
        raise MemoryRepositoryError("existing SQLite backup integrity preflight failed")
    plan: dict[str, object] = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "migration": "sqlite-memory-v3-to-v4-projection-generation-binding",
        "as_of": str(as_of),
        "database": current,
        "existing_backup": backup_fingerprint,
        "target_user_version": PROJECTION_GENERATION_BINDING_SCHEMA_VERSION,
        "tables": sorted(PROJECTION_GENERATION_BINDING_SCHEMA_TABLES),
        "side_effects": {
            "read_only": True,
            "memory_lifecycle_written": False,
            "memory_outbox_written": False,
            "qdrant_written": False,
            "mem0_written": False,
            "automatic_worker_started": False,
        },
    }
    plan["plan_digest"] = hashlib.sha256(_canonical_json(plan).encode("utf-8")).hexdigest()
    return plan


def _verify_plan(plan: Mapping[str, object], database: Path, backup: Path, expected_plan_digest: str) -> None:
    unsigned = dict(plan)
    supplied = str(unsigned.pop("plan_digest", ""))
    actual = hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()
    if supplied != expected_plan_digest or actual != expected_plan_digest:
        raise MemoryRepositoryError("projection binding migration plan digest mismatch")
    for current, planned, label in (
        (_fingerprint(database), plan.get("database"), "authoritative database"),
        (_fingerprint(backup), plan.get("existing_backup"), "existing backup"),
    ):
        if not isinstance(planned, Mapping):
            raise MemoryRepositoryError("projection binding migration plan is malformed")
        for key in ("size", "mtime_ns", "sha256", "wal", "user_version", "tables", "quick_check", "foreign_key_errors"):
            if current.get(key) != planned.get(key):
                raise MemoryRepositoryError(f"{label} changed since migration plan: {key}")


def apply_projection_generation_binding_migration(
    database: Path | str,
    existing_backup: Path | str,
    plan: Mapping[str, object],
    *,
    expected_plan_digest: str,
    confirm_operator: bool = False,
    offline_verified: bool = False,
) -> dict[str, object]:
    """Install provenance tables only after explicit offline/digest gates."""

    if not confirm_operator or not offline_verified:
        raise MemoryRepositoryError("migration requires explicit operator confirmation and offline writer proof")
    target = assert_safe_path(Path(database).expanduser()).resolve()
    backup = assert_safe_path(Path(existing_backup).expanduser()).resolve()
    status = projection_generation_binding_migration_status(target)
    if bool(status.get("ready")):
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "ok": True,
            "action": "already-current",
            "plan_digest": expected_plan_digest,
            "database": status,
            "execution": {"sqlite_written": False, "memory_lifecycle_written": False, "memory_outbox_written": False, "qdrant_written": False, "mem0_written": False},
        }
    _verify_plan(plan, target, backup, expected_plan_digest)
    connection = sqlite3.connect(target, timeout=5.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != GOVERNED_CONSOLIDATION_SCHEMA_VERSION:
            raise MemoryRepositoryError("projection binding migration source schema changed")
        for statement in (
            """
            CREATE TABLE projection_generation_bindings (
                binding_id TEXT PRIMARY KEY,
                binding_digest TEXT NOT NULL UNIQUE,
                project TEXT NOT NULL,
                target_collection TEXT NOT NULL UNIQUE,
                plan_digest TEXT NOT NULL,
                source_basis_digest TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                target_set_digest TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                vector_dimensions INTEGER NOT NULL CHECK (vector_dimensions > 0),
                distance TEXT NOT NULL,
                expected_point_count INTEGER NOT NULL CHECK (expected_point_count > 0),
                parity_digest TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('prepared', 'active', 'rolled_back', 'stale')),
                actor_digest TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                supersedes_binding_id TEXT,
                created_at TEXT NOT NULL,
                prepared_at TEXT NOT NULL,
                activated_at TEXT,
                rolled_back_at TEXT,
                FOREIGN KEY (supersedes_binding_id) REFERENCES projection_generation_bindings(binding_id),
                UNIQUE(project, generation_id, binding_digest)
            )
            """,
            "CREATE UNIQUE INDEX idx_projection_generation_bindings_active_project ON projection_generation_bindings(project) WHERE state = 'active'",
            "CREATE INDEX idx_projection_generation_bindings_generation_state ON projection_generation_bindings(generation_id, state, project)",
            """
            CREATE TABLE projection_generation_binding_events (
                event_id TEXT PRIMARY KEY,
                binding_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('prepared', 'activated', 'rolled_back', 'stale')),
                event_digest TEXT NOT NULL UNIQUE,
                actor_digest TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                details_json TEXT NOT NULL,
                FOREIGN KEY (binding_id) REFERENCES projection_generation_bindings(binding_id)
            )
            """,
            "CREATE INDEX idx_projection_generation_binding_events_binding_time ON projection_generation_binding_events(binding_id, occurred_at, event_id)",
        ):
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version={PROJECTION_GENERATION_BINDING_SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "ok": True,
        "action": "applied",
        "plan_digest": expected_plan_digest,
        "database": projection_generation_binding_migration_status(target),
        "execution": {"sqlite_written": True, "memory_lifecycle_written": False, "memory_outbox_written": False, "qdrant_written": False, "mem0_written": False},
        "rollback": "stop runtime, verify the existing backup, restore offline, then run readiness smoke",
    }


__all__ = [
    "MIGRATION_SCHEMA_VERSION",
    "apply_projection_generation_binding_migration",
    "build_projection_generation_binding_migration_plan",
    "projection_generation_binding_migration_status",
]
