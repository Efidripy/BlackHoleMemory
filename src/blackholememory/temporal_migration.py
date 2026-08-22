"""Operator-gated additive WL-300.2 temporal migration.

The migration is disposable/rollback-safe and never runs during startup.  It
adds nullable temporal fields for legacy rows, a conflict-receipt table and
indexes while preserving all authoritative content, revisions, links,
artifacts and outbox rows byte-for-byte at the logical level.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from .filesystem_boundaries import assert_safe_path
from .freshness_migration import canonical_json, sha256_file
from .memory_repository import FRESHNESS_SCHEMA_TABLES, FRESHNESS_SCHEMA_VERSION
from .temporal_contract import CAPABILITY_KEY, CAPABILITY_VERSION
from .temporal_contract import TEMPORAL_COLUMNS, TEMPORAL_INDEXES, TEMPORAL_SCHEMA_VERSION, TEMPORAL_TABLES
from .temporal_contract import normalize_temporal_fields


TEMPORAL_CONFLICT_INDEXES = frozenset({"idx_temporal_conflicts_project_memory_time"})
SQL_MANIFEST = (
    "ALTER TABLE memories ADD COLUMN observed_at TEXT",
    "ALTER TABLE memories ADD COLUMN observed_at_source TEXT NOT NULL DEFAULT 'legacy-unknown' CHECK "
    "(observed_at_source IN ('explicit','transaction-clock','imported','legacy-unknown'))",
    "ALTER TABLE memories ADD COLUMN valid_from TEXT",
    "ALTER TABLE memories ADD COLUMN valid_to TEXT",
    "ALTER TABLE memories ADD COLUMN open_interval INTEGER NOT NULL DEFAULT 1 CHECK (open_interval IN (0,1))",
    "ALTER TABLE memories ADD COLUMN supersedes_revision_id TEXT",
    "ALTER TABLE memories ADD COLUMN source_episode_id TEXT",
    "ALTER TABLE memories ADD COLUMN source_uri TEXT",
    "ALTER TABLE memories ADD COLUMN source_digest TEXT",
    "CREATE INDEX idx_memories_project_validity_time ON memories(project, valid_from, valid_to, lifecycle, updated_at DESC, memory_id)",
    "CREATE INDEX idx_memories_project_observed_time ON memories(project, observed_at, lifecycle, updated_at DESC, memory_id)",
    "CREATE TABLE memory_temporal_conflicts ("
    "conflict_id TEXT PRIMARY KEY, project TEXT NOT NULL, memory_id TEXT NOT NULL, "
    "conflict_type TEXT NOT NULL CHECK (conflict_type IN ('contradiction','supersession','source-dispute')), "
    "reason TEXT NOT NULL, confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)), "
    "actor TEXT NOT NULL, created_at TEXT NOT NULL, source_episode_id TEXT, source_uri TEXT, "
    "source_digest TEXT, resolution TEXT NOT NULL DEFAULT 'open', payload_json TEXT NOT NULL"
    ")",
    "CREATE INDEX idx_temporal_conflicts_project_memory_time ON memory_temporal_conflicts(project, memory_id, created_at DESC, conflict_id)",
)


class TemporalMigrationError(RuntimeError):
    """Raised when a temporal migration gate or invariant fails closed."""


def _connect_read_only(path: Path) -> sqlite3.Connection:
    resolved = assert_safe_path(path).resolve()
    if not resolved.is_file():
        raise TemporalMigrationError(f"SQLite database is missing: {resolved}")
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _digest_rows(rows: list[sqlite3.Row]) -> str:
    return hashlib.sha256(canonical_json([list(row) for row in rows]).encode("utf-8")).hexdigest()


def _fingerprint(path: str | Path) -> dict[str, Any]:
    resolved = assert_safe_path(path).resolve()
    stat = resolved.stat()
    with closing(_connect_read_only(resolved)) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold()
        fk_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        indexes = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memories)").fetchall()}
        marker = connection.execute("SELECT value FROM memory_store_meta WHERE key = ?", (CAPABILITY_KEY,)).fetchone()
        memories = connection.execute(
            "SELECT memory_id, project, memory_type, lifecycle, title, summary, tags_json, files_json, "
            "session_refs_json, upsert_key, created_at, updated_at, provenance_json, metadata_json, "
            "extra_json, current_revision_id FROM memories ORDER BY memory_id"
        ).fetchall()
        revisions = connection.execute(
            "SELECT revision_id, memory_id, content, content_sha256, created_at, created_by, metadata_json "
            "FROM memory_revisions ORDER BY revision_id"
        ).fetchall()
        outbox = connection.execute(
            "SELECT event_id, aggregate_type, aggregate_id, event_type, event_version, payload_json, status, attempts, "
            "available_at, claimed_at, claim_token, last_error, created_at, updated_at FROM memory_outbox ORDER BY event_id"
        ).fetchall()
        links = connection.execute(
            "SELECT link_id, project, source_id, target_id, relation, created_at, updated_at, metadata_json "
            "FROM memory_links ORDER BY link_id"
        ).fetchall()
        artifacts = connection.execute(
            "SELECT artifact_type, artifact_id, project, memory_id, lifecycle, created_at, updated_at, payload_json "
            "FROM memory_artifacts ORDER BY artifact_type, artifact_id"
        ).fetchall()
        conflicts = (
            connection.execute(
                "SELECT conflict_id, project, memory_id, conflict_type, reason, confidence, actor, created_at, "
                "source_episode_id, source_uri, source_digest, resolution, payload_json "
                "FROM memory_temporal_conflicts ORDER BY conflict_id"
            ).fetchall()
            if "memory_temporal_conflicts" in tables
            else []
        )
        authority = connection.execute(
            "SELECT m.memory_id, m.current_revision_id, r.content_sha256 FROM memories m "
            "JOIN memory_revisions r ON r.revision_id = m.current_revision_id ORDER BY m.memory_id"
        ).fetchall()
    if quick_check != "ok":
        raise TemporalMigrationError(f"SQLite quick_check failed: {quick_check}")
    if fk_errors:
        raise TemporalMigrationError(f"SQLite foreign_key_check returned {len(fk_errors)} rows")
    return {
        "path": str(resolved),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": sha256_file(resolved),
        "user_version": version,
        "quick_check": quick_check,
        "tables": sorted(tables),
        "indexes": sorted(indexes),
        "memory_columns": sorted(columns),
        "capability_version": str(marker[0]) if marker else None,
        "counts": {
            "memories": len(memories),
            "revisions": len(revisions),
            "outbox": len(outbox),
            "links": len(links),
            "artifacts": len(artifacts),
            "conflicts": len(conflicts),
        },
        "authority_digest": _digest_rows(authority),
        "logical_digests": {
            "memories": _digest_rows(memories),
            "revisions": _digest_rows(revisions),
            "outbox": _digest_rows(outbox),
            "links": _digest_rows(links),
            "artifacts": _digest_rows(artifacts),
            "conflicts": _digest_rows(conflicts),
        },
    }


def _validate_source(fingerprint: Mapping[str, Any], *, allow_current: bool) -> None:
    if int(fingerprint.get("user_version") or -1) != FRESHNESS_SCHEMA_VERSION:
        raise TemporalMigrationError(f"expected SQLite user_version {FRESHNESS_SCHEMA_VERSION}")
    if not FRESHNESS_SCHEMA_TABLES.issubset(set(fingerprint.get("tables") or [])):
        raise TemporalMigrationError("freshness v2 tables are missing")
    columns = set(fingerprint.get("memory_columns") or [])
    indexes = set(fingerprint.get("indexes") or [])
    tables = set(fingerprint.get("tables") or [])
    current = TEMPORAL_COLUMNS.issubset(columns) and TEMPORAL_INDEXES.issubset(indexes) and TEMPORAL_TABLES.issubset(tables) and fingerprint.get("capability_version") == CAPABILITY_VERSION
    if current and allow_current:
        return
    present = TEMPORAL_COLUMNS & columns
    if present:
        raise TemporalMigrationError(f"partial temporal schema detected: {sorted(present)}")
    if fingerprint.get("capability_version") is not None:
        raise TemporalMigrationError("temporal capability marker exists without schema")


def _manifest() -> dict[str, Any]:
    return {
        "schema_version": TEMPORAL_SCHEMA_VERSION,
        "columns": sorted(TEMPORAL_COLUMNS),
        "indexes": sorted(TEMPORAL_INDEXES | TEMPORAL_CONFLICT_INDEXES),
        "tables": sorted(TEMPORAL_TABLES),
        "capability_key": CAPABILITY_KEY,
        "capability_version": CAPABILITY_VERSION,
        "sql_sha256": hashlib.sha256(";\n".join(SQL_MANIFEST).encode("utf-8")).hexdigest(),
    }


def build_migration_plan(database: str | Path, existing_backup: str | Path) -> dict[str, Any]:
    database_path = assert_safe_path(database).resolve()
    backup_path = assert_safe_path(existing_backup).resolve()
    if database_path == backup_path:
        raise TemporalMigrationError("database and existing backup must be different files")
    database_fp = _fingerprint(database_path)
    _validate_source(database_fp, allow_current=False)
    backup_fp = _fingerprint(backup_path)
    if backup_fp["counts"] != database_fp["counts"]:
        raise TemporalMigrationError("existing backup does not match authoritative row counts")
    if backup_fp["authority_digest"] != database_fp["authority_digest"]:
        raise TemporalMigrationError("existing backup does not match authoritative content digest")
    if backup_fp["logical_digests"] != database_fp["logical_digests"]:
        raise TemporalMigrationError("existing backup does not match logical snapshot digests")
    plan = {
        "schema_version": TEMPORAL_SCHEMA_VERSION,
        "action": "add-temporal-memory-capability",
        "database": database_fp,
        "existing_full_backup": backup_fp,
        "manifest": _manifest(),
        "execution": {
            "read_only": True,
            "sqlite_written": False,
            "memory_content_written": False,
            "memory_outbox_written": False,
            "qdrant_written": False,
            "mem0_written": False,
        },
    }
    plan["plan_digest"] = hashlib.sha256(canonical_json(plan).encode("utf-8")).hexdigest()
    return plan


def _verify_plan(plan: Mapping[str, Any], database: Path, backup: Path, expected_plan_digest: str) -> None:
    unsigned = dict(plan)
    supplied = str(unsigned.pop("plan_digest", ""))
    actual = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    if supplied != expected_plan_digest or actual != expected_plan_digest:
        raise TemporalMigrationError("migration plan digest mismatch")
    if plan.get("manifest") != _manifest():
        raise TemporalMigrationError("migration manifest changed since plan")
    current = _fingerprint(database)
    planned = plan.get("database") or {}
    for key in ("size", "mtime_ns", "sha256", "user_version", "counts", "authority_digest", "logical_digests"):
        if current.get(key) != planned.get(key):
            raise TemporalMigrationError(f"database changed since plan: {key}")
    backup_current = _fingerprint(backup)
    planned_backup = plan.get("existing_full_backup") or {}
    for key in ("size", "sha256", "quick_check", "counts", "authority_digest", "logical_digests"):
        if backup_current.get(key) != planned_backup.get(key):
            raise TemporalMigrationError(f"existing backup changed since plan: {key}")


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
    if not confirm_operator:
        raise TemporalMigrationError("apply requires explicit operator confirmation")
    if not offline_verified:
        raise TemporalMigrationError("apply requires an offline writer/listener proof")
    database_path = assert_safe_path(database).resolve()
    backup_path = assert_safe_path(existing_backup).resolve()
    if database_path == backup_path:
        raise TemporalMigrationError("database and existing backup must be different files")
    _verify_plan(plan, database_path, backup_path, expected_plan_digest)
    before = _fingerprint(database_path)
    connection = sqlite3.connect(database_path, timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in SQL_MANIFEST:
            connection.execute(statement)
        typed_values: dict[str, dict[str, Any]] = {}
        for row in connection.execute("SELECT memory_id, metadata_json FROM memories ORDER BY memory_id"):
            try:
                metadata = json.loads(str(row[1]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise TemporalMigrationError(f"invalid metadata_json for {row[0]}") from exc
            if not isinstance(metadata, dict):
                raise TemporalMigrationError(f"metadata_json for {row[0]} is not an object")
            fields = normalize_temporal_fields({"metadata": metadata})
            updates = {key: value for key, value in fields.items() if key in TEMPORAL_COLUMNS and value is not None}
            updates["open_interval"] = int(fields["open_interval"])
            if fields["observed_at"] is not None or fields["observed_at_source"] != "legacy-unknown":
                updates["observed_at"] = fields["observed_at"]
                updates["observed_at_source"] = fields["observed_at_source"]
            if updates:
                typed_values[str(row[0])] = updates
        for memory_id, updates in typed_values.items():
            assignments = ", ".join(f"{key} = ?" for key in updates)
            connection.execute(f"UPDATE memories SET {assignments} WHERE memory_id = ?", (*updates.values(), memory_id))
        connection.execute(
            "INSERT OR REPLACE INTO memory_store_meta(key, value) VALUES (?, ?)",
            (CAPABILITY_KEY, CAPABILITY_VERSION),
        )
        if inject_failure:
            raise TemporalMigrationError("injected migration failure")
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memories)").fetchall()}
        indexes = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        if not TEMPORAL_COLUMNS.issubset(columns) or not TEMPORAL_INDEXES.issubset(indexes) or not TEMPORAL_TABLES.issubset(tables):
            raise TemporalMigrationError("temporal migration did not create required schema")
        marker = connection.execute("SELECT value FROM memory_store_meta WHERE key = ?", (CAPABILITY_KEY,)).fetchone()
        if not marker or str(marker[0]) != CAPABILITY_VERSION:
            raise TemporalMigrationError("temporal capability marker is missing or invalid")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    after = _fingerprint(database_path)
    confirmation = {"ok": True, "warnings": []}
    try:
        _validate_source(after, allow_current=True)
        for key in ("user_version", "counts", "authority_digest", "logical_digests"):
            if after.get(key) != before.get(key):
                raise TemporalMigrationError(f"migration changed {key}")
    except TemporalMigrationError as exc:
        confirmation = {"ok": False, "warnings": [str(exc)]}
    return {
        "schema_version": TEMPORAL_SCHEMA_VERSION,
        "ok": True,
        "action": "applied",
        "plan_digest": expected_plan_digest,
        "database": after,
        "existing_full_backup": dict(plan.get("existing_full_backup") or {}),
        "post_commit_confirmation": confirmation,
        "execution": {
            "sqlite_written": True,
            "memory_content_written": False,
            "memory_outbox_written": False,
            "qdrant_written": False,
            "mem0_written": False,
        },
        "rollback": "disable the temporal feature; for schema rollback stop runtime and restore the verified backup offline",
    }


__all__ = [
    "SQL_MANIFEST",
    "TEMPORAL_CONFLICT_INDEXES",
    "TemporalMigrationError",
    "apply_migration",
    "build_migration_plan",
]
