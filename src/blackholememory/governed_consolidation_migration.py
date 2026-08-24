"""Explicit additive migration for governed consolidation proposal tables."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .filesystem_boundaries import assert_safe_path
from .governed_consolidation import GOVERNED_CONSOLIDATION_CAPABILITY_KEY
from .governed_consolidation import GOVERNED_CONSOLIDATION_CAPABILITY_VERSION
from .memory_repository import FRESHNESS_SCHEMA_VERSION
from .memory_repository import MemoryRepositoryError


GOVERNED_CONSOLIDATION_SQLITE_SCHEMA_VERSION = 3
MIGRATION_SCHEMA_VERSION = "bhm.governed-consolidation-migration.v1"
_BASE_TABLES = frozenset({"memory_store_meta", "memories", "memory_revisions", "memory_artifacts", "memory_links", "memory_outbox"})
_MIGRATION_TABLES = frozenset({"governed_consolidation_proposals", "governed_consolidation_events"})


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_sidecar_fingerprint(path: Path) -> dict[str, object] | None:
    """Fingerprint SQLite WAL state without touching the authoritative store."""

    if not path.exists():
        return None
    return {"size": int(path.stat().st_size), "sha256": _sha256_file(path)}


def _fingerprint(path: Path) -> dict[str, object]:
    target = assert_safe_path(path).resolve()
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
        # SQLite WAL commits may leave the main database bytes unchanged.
        # Bind the plan to the sidecar too, otherwise a post-plan authority
        # write could evade an ordinary file-only hash comparison.
        "wal": _optional_sidecar_fingerprint(Path(f"{target}-wal")),
        "user_version": version,
        "tables": sorted(tables),
        "quick_check": quick_check,
        "foreign_key_errors": len(foreign_keys),
    }


def governed_consolidation_migration_status(path: Path | str) -> dict[str, object]:
    target = Path(path).expanduser()
    assert_safe_path(target)
    if not target.exists():
        return {"ready": False, "reason": "database_missing", "path": str(target)}
    uri = f"file:{target.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            meta = connection.execute("SELECT value FROM memory_store_meta WHERE key = ?", (GOVERNED_CONSOLIDATION_CAPABILITY_KEY,)).fetchone() if "memory_store_meta" in tables else None
            return {"ready": bool(meta and str(meta[0]) == GOVERNED_CONSOLIDATION_CAPABILITY_VERSION and {"governed_consolidation_proposals", "governed_consolidation_events"}.issubset(tables)), "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]), "path": str(target)}
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return {"ready": False, "reason": type(exc).__name__, "path": str(target)}


def build_governed_consolidation_migration_plan(
    database: Path | str,
    existing_backup: Path | str,
    *,
    as_of: str,
) -> dict[str, object]:
    """Build a read-only, exact-database migration plan."""

    try:
        datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryRepositoryError("as_of must be an ISO-8601 timestamp") from exc
    target = assert_safe_path(Path(database).expanduser()).resolve()
    backup = assert_safe_path(Path(existing_backup).expanduser()).resolve()
    if target == backup or not backup.is_file():
        raise MemoryRepositoryError("a distinct existing SQLite backup is required")
    current = _fingerprint(target)
    if current["user_version"] != FRESHNESS_SCHEMA_VERSION or not _BASE_TABLES.issubset(set(current["tables"])):
        raise MemoryRepositoryError("governed consolidation migration requires a verified v2 SQLite authority store")
    if current["quick_check"] != "ok" or current["foreign_key_errors"]:
        raise MemoryRepositoryError("authoritative SQLite integrity preflight failed")
    backup_fingerprint = _fingerprint(backup)
    if backup_fingerprint["quick_check"] != "ok" or backup_fingerprint["foreign_key_errors"]:
        raise MemoryRepositoryError("existing SQLite backup integrity preflight failed")
    plan = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "migration": "sqlite-memory-v2-to-v3-governed-consolidation",
        "as_of": str(as_of),
        "database": current,
        "existing_backup": backup_fingerprint,
        "target_user_version": GOVERNED_CONSOLIDATION_SQLITE_SCHEMA_VERSION,
        "tables": sorted(_MIGRATION_TABLES),
        "side_effects": {"read_only": True, "memory_lifecycle_written": False, "memory_outbox_written": False, "qdrant_written": False, "mem0_written": False, "automatic_worker_started": False},
    }
    plan["plan_digest"] = hashlib.sha256(_canonical_json(plan).encode("utf-8")).hexdigest()
    return plan


def _verify_plan(plan: dict[str, object], database: Path, backup: Path, expected_plan_digest: str) -> None:
    supplied = dict(plan)
    digest = str(supplied.pop("plan_digest", ""))
    if digest != expected_plan_digest or hashlib.sha256(_canonical_json(supplied).encode("utf-8")).hexdigest() != expected_plan_digest:
        raise MemoryRepositoryError("governed consolidation migration plan digest mismatch")
    for actual, planned, label in ((_fingerprint(database), plan.get("database"), "authoritative database"), (_fingerprint(backup), plan.get("existing_backup"), "existing backup")):
        if not isinstance(planned, dict):
            raise MemoryRepositoryError("governed consolidation migration plan is malformed")
        for key in ("size", "mtime_ns", "sha256", "wal", "user_version", "tables", "quick_check", "foreign_key_errors"):
            if actual.get(key) != planned.get(key):
                raise MemoryRepositoryError(f"{label} changed since migration plan: {key}")


def apply_governed_consolidation_migration(
    database: Path | str,
    existing_backup: Path | str,
    plan: dict[str, object],
    *,
    expected_plan_digest: str,
    confirm_operator: bool = False,
    offline_verified: bool = False,
) -> dict[str, object]:
    """Install proposal tables after backup, digest and offline-writer gates."""

    if not confirm_operator or not offline_verified:
        raise MemoryRepositoryError("migration requires explicit operator confirmation and offline writer proof")

    target = Path(database).expanduser()
    assert_safe_path(target)
    if not target.exists():
        raise MemoryRepositoryError("governed consolidation migration requires an existing authoritative SQLite store")
    backup = assert_safe_path(Path(existing_backup).expanduser())
    status = governed_consolidation_migration_status(target)
    if bool(status.get("ready")):
        return {"schema_version": MIGRATION_SCHEMA_VERSION, "ok": True, "action": "already-current", "plan_digest": expected_plan_digest, "database": status, "execution": {"sqlite_written": False, "memory_lifecycle_written": False, "memory_outbox_written": False, "qdrant_written": False, "mem0_written": False, "automatic_worker_started": False}}
    _verify_plan(plan, target.resolve(), backup.resolve(), expected_plan_digest)
    connection = sqlite3.connect(target, timeout=5.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current not in {FRESHNESS_SCHEMA_VERSION, GOVERNED_CONSOLIDATION_SQLITE_SCHEMA_VERSION}:
            raise MemoryRepositoryError(f"unsupported source schema {current}")
        required = {"memory_store_meta", "memories", "memory_revisions", "memory_outbox"}
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not required.issubset(tables):
            raise MemoryRepositoryError("authoritative memory schema is incomplete")
        # ``sqlite3.Connection.executescript`` issues an implicit COMMIT
        # before executing its input.  Execute each DDL statement separately
        # so these tables, the capability marker and user_version stay inside
        # the explicit transaction opened above.
        for statement in (
            """
            CREATE TABLE IF NOT EXISTS governed_consolidation_proposals (
                proposal_id TEXT PRIMARY KEY,
                proposal_digest TEXT NOT NULL,
                project TEXT NOT NULL,
                operation TEXT NOT NULL CHECK (operation IN ('no_op', 'create', 'revise', 'supersede', 'archive', 'link')),
                status TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected', 'applied', 'stale', 'failed')),
                basis_json TEXT NOT NULL,
                candidate_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
                conflicts_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                proposal_json TEXT NOT NULL,
                requires_human_approval INTEGER NOT NULL CHECK (requires_human_approval IN (0, 1)),
                approved_at TEXT,
                approved_by_digest TEXT,
                rejected_at TEXT,
                rejected_by_digest TEXT,
                applied_at TEXT,
                stale_at TEXT,
                failure_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project, proposal_digest)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_governed_consolidation_project_status_time
                ON governed_consolidation_proposals(project, status, created_at DESC, proposal_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS governed_consolidation_events (
                event_id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL,
                action TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (proposal_id) REFERENCES governed_consolidation_proposals(proposal_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_governed_consolidation_events_proposal_time
                ON governed_consolidation_events(proposal_id, created_at, event_id)
            """,
        ):
            connection.execute(statement)
        connection.execute("INSERT OR REPLACE INTO memory_store_meta(key, value) VALUES (?, ?)", (GOVERNED_CONSOLIDATION_CAPABILITY_KEY, GOVERNED_CONSOLIDATION_CAPABILITY_VERSION))
        connection.execute(f"PRAGMA user_version={GOVERNED_CONSOLIDATION_SQLITE_SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"schema_version": MIGRATION_SCHEMA_VERSION, "ok": True, "action": "applied", "plan_digest": expected_plan_digest, "database": governed_consolidation_migration_status(target), "execution": {"sqlite_written": True, "memory_lifecycle_written": False, "memory_outbox_written": False, "qdrant_written": False, "mem0_written": False, "automatic_worker_started": False}, "rollback": "stop runtime, verify the existing backup, restore offline, then run readiness smoke"}


__all__ = ["GOVERNED_CONSOLIDATION_SQLITE_SCHEMA_VERSION", "MIGRATION_SCHEMA_VERSION", "apply_governed_consolidation_migration", "build_governed_consolidation_migration_plan", "governed_consolidation_migration_status"]
