"""Operator-gated additive SQLite access-index migration for WL-300.7.

The index is a derived, bounded read path for high-signal identifiers.  It is
not an authority for lifecycle, content or project membership: the runtime
still hydrates records from SQLite before returning a candidate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from .exact_identifier_retrieval import EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY
from .exact_identifier_retrieval import EXACT_IDENTIFIER_INDEX_CAPABILITY_VERSION
from .exact_identifier_retrieval import EXACT_IDENTIFIER_INDEX_TABLE
from .exact_identifier_retrieval import exact_identifier_record_text
from .exact_identifier_retrieval import exact_identifier_tokens
from .filesystem_boundaries import assert_safe_path
from .freshness_migration import canonical_json
from .memory_class_migration import _connection_logical_digests
from .memory_class_migration import _database_fingerprint
from .memory_repository import FRESHNESS_SCHEMA_TABLES
from .memory_repository import FRESHNESS_SCHEMA_VERSION


MIGRATION_SCHEMA_VERSION = "bhm.exact-identifier-index-migration.v1"
INDEX_NAME = "idx_memory_identifier_tokens_memory_id"
SQL_MANIFEST = (
    "CREATE TABLE memory_identifier_tokens ("
    "project TEXT NOT NULL, token TEXT NOT NULL, memory_id TEXT NOT NULL, "
    "PRIMARY KEY(project, token, memory_id), "
    "FOREIGN KEY(memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE)",
    "CREATE INDEX idx_memory_identifier_tokens_memory_id "
    "ON memory_identifier_tokens(memory_id)",
)


class ExactIdentifierIndexMigrationError(RuntimeError):
    """Raised when an index migration precondition or postcondition fails."""


def _connect_read_only(path: Path) -> sqlite3.Connection:
    resolved = assert_safe_path(path).resolve()
    if not resolved.is_file():
        raise ExactIdentifierIndexMigrationError(f"SQLite database is missing: {resolved}")
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _index_state(connection: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(connection, EXACT_IDENTIFIER_INDEX_TABLE):
        return {"row_count": 0, "digest": None}
    rows = connection.execute(
        f"SELECT project, token, memory_id FROM {EXACT_IDENTIFIER_INDEX_TABLE} "
        "ORDER BY project, token, memory_id"
    ).fetchall()
    return {
        "row_count": len(rows),
        "digest": hashlib.sha256(canonical_json([list(row) for row in rows]).encode("utf-8")).hexdigest(),
    }


def _row_record(row: sqlite3.Row) -> dict[str, Any]:
    try:
        tags = json.loads(str(row["tags_json"]))
        files = json.loads(str(row["files_json"]))
        metadata = json.loads(str(row["metadata_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExactIdentifierIndexMigrationError(
            f"invalid identifier source JSON for {row['memory_id']}"
        ) from exc
    if not isinstance(tags, list) or not isinstance(files, list) or not isinstance(metadata, dict):
        raise ExactIdentifierIndexMigrationError(
            f"invalid identifier source shape for {row['memory_id']}"
        )
    return {
        "content": str(row["content"] or ""),
        "title": str(row["title"] or ""),
        "summary": str(row["summary"] or ""),
        "upsert_key": str(row["upsert_key"] or ""),
        "tags": tags,
        "files": files,
        "metadata": metadata,
    }


def _expected_index_rows(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = connection.execute(
        """
        SELECT m.memory_id, m.project, m.lifecycle, m.title, m.summary, m.tags_json,
               m.files_json, m.upsert_key, m.metadata_json, r.content
        FROM memories AS m
        JOIN memory_revisions AS r ON r.revision_id = m.current_revision_id
        WHERE m.lifecycle = 'active'
        ORDER BY m.project, m.memory_id
        """
    ).fetchall()
    expected: list[tuple[str, str, str]] = []
    for row in rows:
        record = _row_record(row)
        for token in exact_identifier_tokens(exact_identifier_record_text(record)):
            expected.append((str(row["project"]), token, str(row["memory_id"])))
    return sorted(set(expected))


def _expected_index_state(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = _expected_index_rows(connection)
    return {
        "row_count": len(rows),
        "digest": hashlib.sha256(canonical_json([list(row) for row in rows]).encode("utf-8")).hexdigest(),
    }


def _fingerprint(path: str | Path) -> dict[str, Any]:
    resolved = assert_safe_path(path).resolve()
    base = _database_fingerprint(resolved)
    with closing(_connect_read_only(resolved)) as connection:
        marker = connection.execute(
            "SELECT value FROM memory_store_meta WHERE key = ?",
            (EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY,),
        ).fetchone()
        base["exact_identifier_capability_version"] = str(marker[0]) if marker else None
        base["exact_identifier_index"] = _index_state(connection)
    return base


def _validate_source(fingerprint: Mapping[str, Any], *, allow_current: bool) -> None:
    if int(fingerprint.get("user_version") or -1) != FRESHNESS_SCHEMA_VERSION:
        raise ExactIdentifierIndexMigrationError(
            f"expected SQLite user_version {FRESHNESS_SCHEMA_VERSION}, found {fingerprint.get('user_version')}"
        )
    if not FRESHNESS_SCHEMA_TABLES.issubset(set(fingerprint.get("tables") or [])):
        raise ExactIdentifierIndexMigrationError("required additive memory schema is missing")
    marker = fingerprint.get("exact_identifier_capability_version")
    indexes = set(fingerprint.get("indexes") or [])
    table_present = EXACT_IDENTIFIER_INDEX_TABLE in set(fingerprint.get("tables") or [])
    current = (
        table_present
        and INDEX_NAME in indexes
        and marker == EXACT_IDENTIFIER_INDEX_CAPABILITY_VERSION
    )
    if current and allow_current:
        return
    if table_present or INDEX_NAME in indexes or marker is not None:
        raise ExactIdentifierIndexMigrationError("partial or already-applied exact identifier index schema")


def _manifest() -> dict[str, Any]:
    return {
        "table": EXACT_IDENTIFIER_INDEX_TABLE,
        "index": INDEX_NAME,
        "capability_key": EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY,
        "capability_version": EXACT_IDENTIFIER_INDEX_CAPABILITY_VERSION,
        "sql_sha256": hashlib.sha256(";\n".join(SQL_MANIFEST).encode("utf-8")).hexdigest(),
        "preserve_user_version": FRESHNESS_SCHEMA_VERSION,
    }


def build_migration_plan(database: str | Path, existing_backup: str | Path) -> dict[str, Any]:
    """Return a read-only plan bound to exact SQLite and backup snapshots."""

    database_path = assert_safe_path(database).resolve()
    backup_path = assert_safe_path(existing_backup).resolve()
    if database_path == backup_path:
        raise ExactIdentifierIndexMigrationError("database and existing backup must be different files")
    database_fingerprint = _fingerprint(database_path)
    backup_fingerprint = _fingerprint(backup_path)
    _validate_source(database_fingerprint, allow_current=False)
    if backup_fingerprint["counts"] != database_fingerprint["counts"]:
        raise ExactIdentifierIndexMigrationError("existing backup does not match authoritative row counts")
    if backup_fingerprint["authority_digest"] != database_fingerprint["authority_digest"]:
        raise ExactIdentifierIndexMigrationError("existing backup does not match authoritative content digest")
    if backup_fingerprint["logical_digests"] != database_fingerprint["logical_digests"]:
        raise ExactIdentifierIndexMigrationError("existing backup does not match logical snapshot digests")
    with closing(_connect_read_only(database_path)) as connection:
        expected_index = _expected_index_state(connection)
    plan = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "action": "add-exact-identifier-access-index",
        "database": database_fingerprint,
        "existing_full_backup": backup_fingerprint,
        "manifest": _manifest(),
        "expected_index": expected_index,
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
    supplied_digest = str(unsigned.pop("plan_digest", ""))
    actual_digest = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    if supplied_digest != expected_plan_digest or actual_digest != expected_plan_digest:
        raise ExactIdentifierIndexMigrationError("migration plan digest mismatch")
    if plan.get("manifest") != _manifest():
        raise ExactIdentifierIndexMigrationError("migration manifest changed since plan")
    current = _fingerprint(database)
    planned = plan.get("database") or {}
    for key in ("size", "mtime_ns", "sha256", "user_version", "counts", "authority_digest", "logical_digests"):
        if current.get(key) != planned.get(key):
            raise ExactIdentifierIndexMigrationError(f"database changed since plan: {key}")
    _validate_source(current, allow_current=False)
    current_backup = _fingerprint(backup)
    planned_backup = plan.get("existing_full_backup") or {}
    for key in ("size", "sha256", "quick_check", "foreign_key_errors", "counts", "authority_digest", "logical_digests"):
        if current_backup.get(key) != planned_backup.get(key):
            raise ExactIdentifierIndexMigrationError(f"existing backup changed since plan: {key}")


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
    """Apply the additive access-index transaction after strict operator gates."""

    if not confirm_operator:
        raise ExactIdentifierIndexMigrationError("apply requires explicit operator confirmation")
    if not offline_verified:
        raise ExactIdentifierIndexMigrationError("apply requires an offline writer/listener proof")
    database_path = assert_safe_path(database).resolve()
    backup_path = assert_safe_path(existing_backup).resolve()
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
        rows = _expected_index_rows(connection)
        connection.executemany(
            f"INSERT INTO {EXACT_IDENTIFIER_INDEX_TABLE}(project, token, memory_id) VALUES (?, ?, ?)", rows
        )
        connection.execute(
            "INSERT OR REPLACE INTO memory_store_meta(key, value) VALUES (?, ?)",
            (EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY, EXACT_IDENTIFIER_INDEX_CAPABILITY_VERSION),
        )
        if inject_failure:
            raise ExactIdentifierIndexMigrationError("injected migration failure")
        schema_tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        schema_indexes = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        marker = connection.execute(
            "SELECT value FROM memory_store_meta WHERE key = ?", (EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY,)
        ).fetchone()
        if EXACT_IDENTIFIER_INDEX_TABLE not in schema_tables or INDEX_NAME not in schema_indexes:
            raise ExactIdentifierIndexMigrationError("migration did not create exact identifier index schema")
        if marker is None or str(marker[0]) != EXACT_IDENTIFIER_INDEX_CAPABILITY_VERSION:
            raise ExactIdentifierIndexMigrationError("migration capability marker is missing or invalid")
        if _index_state(connection) != dict(plan.get("expected_index") or {}):
            raise ExactIdentifierIndexMigrationError("migration backfill does not match expected identifier index")
        logical = _connection_logical_digests(connection)
        if logical["counts"] != before["counts"] or logical["authority_digest"] != before["authority_digest"]:
            raise ExactIdentifierIndexMigrationError("migration changed authoritative memory content")
        if logical["logical_digests"] != before["logical_digests"]:
            raise ExactIdentifierIndexMigrationError("migration changed logical memories, revisions, or outbox")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    after = _fingerprint(database_path)
    _validate_source(after, allow_current=True)
    if after["authority_digest"] != before["authority_digest"] or after["logical_digests"] != before["logical_digests"]:
        raise ExactIdentifierIndexMigrationError("post-commit confirmation changed authoritative state")
    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "ok": True,
        "action": "applied",
        "plan_digest": expected_plan_digest,
        "database": after,
        "existing_full_backup": dict(plan.get("existing_full_backup") or {}),
        "expected_index": dict(plan.get("expected_index") or {}),
        "execution": {
            "sqlite_written": True,
            "memory_content_written": False,
            "memory_outbox_written": False,
            "qdrant_written": False,
            "mem0_written": False,
        },
        "rollback": "disable exact retrieval; for schema rollback stop runtime and restore the verified backup offline",
    }


__all__ = [
    "EXACT_IDENTIFIER_INDEX_CAPABILITY_KEY",
    "EXACT_IDENTIFIER_INDEX_CAPABILITY_VERSION",
    "EXACT_IDENTIFIER_INDEX_TABLE",
    "ExactIdentifierIndexMigrationError",
    "INDEX_NAME",
    "MIGRATION_SCHEMA_VERSION",
    "SQL_MANIFEST",
    "apply_migration",
    "build_migration_plan",
]
