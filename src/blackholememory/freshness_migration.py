"""Fail-closed WL-295.2 additive SQLite migration.

The migration is intentionally separate from normal repository startup.  It
adds review-only freshness candidate state, never changes a memory lifecycle,
never appends to ``memory_outbox``, and never contacts Qdrant or Mem0.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .filesystem_boundaries import assert_safe_path
from .memory_repository import FRESHNESS_SCHEMA_TABLES
from .memory_repository import MEMORY_STORE_SCHEMA_LATEST_VERSION
from .memory_repository import MEMORY_STORE_SCHEMA_VERSION


MIGRATION_SCHEMA_VERSION = "bhm.freshness-migration.v1"
POLICY_VERSION = "bhm.freshness-candidate-policy.v1"
REASON_CODES = (
    "source_changed",
    "superseded_by_revision",
    "contradicted",
    "unreferenced",
    "age_threshold_reached",
)
CANDIDATE_STATES = ("open", "dismissed", "accepted")
EVENT_ACTIONS = ("detected", "dismissed", "accepted")
BASE_TABLES = frozenset(
    {"memory_store_meta", "memories", "memory_revisions", "memory_artifacts", "memory_links", "memory_outbox"}
)
MIGRATION_TABLES = frozenset(FRESHNESS_SCHEMA_TABLES)

SQL_MANIFEST = """
CREATE TABLE freshness_candidates (
    candidate_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    reason_code TEXT NOT NULL CHECK (reason_code IN ('source_changed','superseded_by_revision','contradicted','unreferenced','age_threshold_reached')),
    source_revision_id TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    evidence_summary_json TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open','dismissed','accepted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project, memory_id, reason_code, evidence_digest, policy_version)
);
CREATE INDEX idx_freshness_candidates_project_state_time
    ON freshness_candidates(project, state, updated_at DESC, candidate_id);
CREATE INDEX idx_freshness_candidates_memory
    ON freshness_candidates(project, memory_id, updated_at DESC);
CREATE TABLE freshness_candidate_events (
    event_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    project TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('detected','dismissed','accepted')),
    decision_note TEXT NOT NULL,
    caller_ref_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY (candidate_id) REFERENCES freshness_candidates(candidate_id)
);
CREATE INDEX idx_freshness_candidate_events_project_time
    ON freshness_candidate_events(project, occurred_at DESC, event_id);
CREATE INDEX idx_freshness_candidate_events_candidate_time
    ON freshness_candidate_events(candidate_id, occurred_at DESC, event_id);
CREATE TABLE freshness_scan_state (
    project TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    watermark TEXT,
    last_completed_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    latest_duration_ms REAL,
    last_error_category TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project, policy_version)
);
""".strip()


class FreshnessMigrationError(RuntimeError):
    """Raised when a migration precondition or postcondition fails."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: str | Path) -> str:
    resolved = assert_safe_path(path).resolve()
    if not resolved.is_file():
        raise FreshnessMigrationError(f"file is missing: {resolved}")
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise FreshnessMigrationError(f"file cannot be hashed: {resolved}") from exc
    return digest.hexdigest()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    resolved = assert_safe_path(path).resolve()
    if not resolved.is_file():
        raise FreshnessMigrationError(f"SQLite database is missing: {resolved}")
    try:
        connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection
    except sqlite3.Error as exc:
        raise FreshnessMigrationError(f"SQLite database is unreadable: {resolved}") from exc


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _index_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}


def _database_fingerprint(path: Path, *, require_base_v1: bool = True) -> dict[str, Any]:
    resolved = assert_safe_path(path).resolve()
    stat = resolved.stat()
    with _connect_read_only(resolved) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold()
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()]
        tables = _table_names(connection)
        indexes = _index_names(connection)
        if require_base_v1:
            if version != MEMORY_STORE_SCHEMA_VERSION:
                raise FreshnessMigrationError(f"expected SQLite user_version {MEMORY_STORE_SCHEMA_VERSION}, found {version}")
            if not BASE_TABLES.issubset(tables):
                raise FreshnessMigrationError(f"base memory tables are missing: {sorted(BASE_TABLES - tables)}")
            if tables & MIGRATION_TABLES:
                raise FreshnessMigrationError(f"freshness tables already exist: {sorted(tables & MIGRATION_TABLES)}")
        if quick_check != "ok":
            raise FreshnessMigrationError(f"SQLite quick_check failed: {quick_check}")
        if foreign_keys:
            raise FreshnessMigrationError(f"SQLite foreign_key_check returned {len(foreign_keys)} rows")
    return {
        "path": str(resolved),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": sha256_file(resolved),
        "user_version": version,
        "quick_check": quick_check,
        "foreign_key_errors": len(foreign_keys),
        "tables": sorted(tables),
        "indexes": sorted(indexes),
    }


def _expected_manifest() -> dict[str, Any]:
    return {
        "tables": sorted(MIGRATION_TABLES),
        "indexes": sorted(
            {
                "idx_freshness_candidates_project_state_time",
                "idx_freshness_candidates_memory",
                "idx_freshness_candidate_events_project_time",
                "idx_freshness_candidate_events_candidate_time",
            }
        ),
        "sql_sha256": hashlib.sha256(SQL_MANIFEST.encode("utf-8")).hexdigest(),
        "target_user_version": MEMORY_STORE_SCHEMA_LATEST_VERSION,
    }


def build_migration_plan(database: str | Path, existing_backup: str | Path, *, as_of: str) -> dict[str, Any]:
    """Build a read-only plan from exact database and existing-backup fingerprints."""

    database_path = assert_safe_path(database).resolve()
    backup_path = assert_safe_path(existing_backup).resolve()
    if database_path == backup_path:
        raise FreshnessMigrationError("existing full backup must be distinct from authoritative SQLite")
    if not backup_path.is_file():
        raise FreshnessMigrationError(f"existing full backup is missing: {backup_path}")
    try:
        datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
    except ValueError as exc:
        raise FreshnessMigrationError("as_of must be an ISO-8601 timestamp") from exc
    database_fingerprint = _database_fingerprint(database_path)
    backup_fingerprint = _database_fingerprint(backup_path, require_base_v1=False)
    if backup_fingerprint["quick_check"] != "ok" or backup_fingerprint["foreign_key_errors"]:
        raise FreshnessMigrationError("existing full backup failed integrity verification")
    plan = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "migration": "sqlite-memory-v1-to-v2-freshness-candidates",
        "as_of": str(as_of),
        "database": database_fingerprint,
        "existing_full_backup": backup_fingerprint,
        "expected": _expected_manifest(),
        "policy_version": POLICY_VERSION,
        "side_effects": {"read_only": True, "sqlite_written": False, "outbox_written": False, "qdrant_written": False, "mem0_written": False},
    }
    plan["plan_digest"] = hashlib.sha256(canonical_json(plan).encode("utf-8")).hexdigest()
    return plan


def _verify_plan(plan: Mapping[str, Any], database: Path, backup: Path, expected_plan_digest: str) -> None:
    payload = dict(plan)
    actual_digest = str(payload.pop("plan_digest") or "")
    if actual_digest != expected_plan_digest or hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest() != expected_plan_digest:
        raise FreshnessMigrationError("migration plan digest mismatch")
    current = _database_fingerprint(database)
    planned = plan.get("database") or {}
    for key in ("size", "mtime_ns", "sha256", "user_version", "quick_check", "foreign_key_errors", "tables", "indexes"):
        if current.get(key) != planned.get(key):
            raise FreshnessMigrationError(f"authoritative database changed since plan: {key}")
    backup_fingerprint = _database_fingerprint(backup, require_base_v1=False)
    planned_backup = plan.get("existing_full_backup") or {}
    for key in ("size", "sha256", "quick_check", "foreign_key_errors"):
        if backup_fingerprint.get(key) != planned_backup.get(key):
            raise FreshnessMigrationError(f"existing backup changed since plan: {key}")


def apply_migration(
    database: str | Path,
    existing_backup: str | Path,
    plan: Mapping[str, Any],
    *,
    expected_plan_digest: str,
    confirm_operator: bool = False,
    offline_verified: bool = False,
    inject_failure: bool = False,
) -> dict[str, Any]:
    """Apply one exact additive transaction after all operator gates pass."""

    if not confirm_operator:
        raise FreshnessMigrationError("apply requires explicit operator confirmation")
    if not offline_verified:
        raise FreshnessMigrationError("apply requires an offline writer/listener proof")
    database_path = assert_safe_path(database).resolve()
    backup_path = assert_safe_path(existing_backup).resolve()
    current = _database_fingerprint(database, require_base_v1=False)
    if current["user_version"] == MEMORY_STORE_SCHEMA_LATEST_VERSION and MIGRATION_TABLES.issubset(set(current["tables"])):
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "ok": True,
            "action": "already-current",
            "plan_digest": expected_plan_digest,
            "database": current,
            "execution": {"sqlite_written": False, "memory_lifecycle_written": False, "memory_outbox_written": False, "qdrant_written": False, "mem0_written": False, "automatic_scanner_started": False},
        }
    _verify_plan(plan, database_path, backup_path, expected_plan_digest)
    connection = sqlite3.connect(database_path, timeout=30.0, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in SQL_MANIFEST.split(";\n"):
            statement = statement.strip()
            if statement:
                connection.execute(statement)
        connection.execute(f"PRAGMA user_version={MEMORY_STORE_SCHEMA_LATEST_VERSION}")
        if inject_failure:
            raise FreshnessMigrationError("injected migration failure")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    after = _database_fingerprint(database, require_base_v1=False)
    if after["user_version"] != MEMORY_STORE_SCHEMA_LATEST_VERSION or not MIGRATION_TABLES.issubset(set(after["tables"])):
        raise FreshnessMigrationError("post-migration schema verification failed")
    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "ok": True,
        "action": "applied",
        "plan_digest": expected_plan_digest,
        "database": after,
        "existing_full_backup": dict(plan.get("existing_full_backup") or {}),
        "execution": {"sqlite_written": True, "memory_lifecycle_written": False, "memory_outbox_written": False, "qdrant_written": False, "mem0_written": False, "automatic_scanner_started": False},
        "rollback": "stop runtime, verify existing full backup, restore it offline, then run readiness smoke",
    }


__all__ = [
    "BASE_TABLES",
    "CANDIDATE_STATES",
    "EVENT_ACTIONS",
    "FreshnessMigrationError",
    "MIGRATION_SCHEMA_VERSION",
    "MIGRATION_TABLES",
    "POLICY_VERSION",
    "REASON_CODES",
    "SQL_MANIFEST",
    "apply_migration",
    "build_migration_plan",
    "canonical_json",
    "sha256_file",
]
