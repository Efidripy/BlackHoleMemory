"""Operator-gated additive WL-300.1 memory-class migration.

The migration keeps ``PRAGMA user_version`` at the current memory-store
version so older BHM binaries can continue reading the additive columns.  A
versioned capability marker in ``memory_store_meta`` is the authority for this
optional contract.  Normal startup never applies this migration.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from .filesystem_boundaries import assert_safe_path
from .freshness_migration import canonical_json
from .freshness_migration import sha256_file
from .memory_repository import FRESHNESS_SCHEMA_TABLES
from .memory_repository import FRESHNESS_SCHEMA_VERSION
from .typed_memory_contract import CAPABILITY_KEY
from .typed_memory_contract import CAPABILITY_VERSION
from .typed_memory_contract import TYPED_MEMORY_INDEXES


MIGRATION_SCHEMA_VERSION = "bhm.typed-memory-migration.v1"
MEMORY_CLASS_COLUMNS = (
    "memory_class",
    "memory_class_source",
    "memory_class_confidence",
)
EVENT_ROLE_COLUMNS = (
    "event_role",
    "event_role_version",
)
TYPED_MEMORY_COLUMNS = (*MEMORY_CLASS_COLUMNS, *EVENT_ROLE_COLUMNS)
MEMORY_CLASS_INDEX = "idx_memories_project_class_lifecycle_time"
EVENT_ROLE_INDEX = "idx_memories_project_event_role_lifecycle_time"

SQL_MANIFEST = (
    "ALTER TABLE memories ADD COLUMN memory_class TEXT NOT NULL "
    "DEFAULT 'unclassified' CHECK (memory_class IN "
    "('episodic','semantic','procedural','working','unclassified'))",
    "ALTER TABLE memories ADD COLUMN memory_class_source TEXT NOT NULL "
    "DEFAULT 'legacy-default' CHECK (memory_class_source IN "
    "('legacy-default','request-default','caller-explicit',"
    "'deterministic-rule','review-confirmed'))",
    "ALTER TABLE memories ADD COLUMN memory_class_confidence REAL "
    "CHECK (memory_class_confidence IS NULL OR "
    "(memory_class_confidence >= 0 AND memory_class_confidence <= 1))",
    "CREATE INDEX idx_memories_project_class_lifecycle_time "
    "ON memories(project, memory_class, lifecycle, updated_at DESC, memory_id)",
    "ALTER TABLE memories ADD COLUMN event_role TEXT NOT NULL "
    "DEFAULT 'unclassified' CHECK (event_role IN "
    "('fact','decision','qa','trace','feedback','skill_run','unclassified'))",
    "ALTER TABLE memories ADD COLUMN event_role_version TEXT NOT NULL "
    "DEFAULT '1' CHECK (event_role_version = '1')",
    "CREATE INDEX idx_memories_project_event_role_lifecycle_time "
    "ON memories(project, event_role, lifecycle, updated_at DESC, memory_id)",
)


class MemoryClassMigrationError(RuntimeError):
    """Raised when a migration precondition or postcondition fails closed."""


def _connect_read_only(path: Path) -> sqlite3.Connection:
    resolved = assert_safe_path(path).resolve()
    if not resolved.is_file():
        raise MemoryClassMigrationError(f"SQLite database is missing: {resolved}")
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _database_fingerprint(path: str | Path) -> dict[str, Any]:
    resolved = assert_safe_path(path).resolve()
    stat = resolved.stat()
    with closing(_connect_read_only(resolved)) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(memories)").fetchall()
        }
        capability_row = connection.execute(
            "SELECT value FROM memory_store_meta WHERE key = ?",
            (CAPABILITY_KEY,),
        ).fetchone()
        counts = {
            "memories": int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
            "revisions": int(connection.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0]),
            "outbox": int(connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0]),
        }
        authority_rows = connection.execute(
            """
            SELECT m.memory_id, m.current_revision_id, r.content_sha256
            FROM memories AS m
            JOIN memory_revisions AS r
              ON r.revision_id = m.current_revision_id
            ORDER BY m.memory_id
            """
        ).fetchall()
        memory_rows = connection.execute(
            "SELECT memory_id, project, memory_type, lifecycle, title, summary, tags_json, files_json, "
            "session_refs_json, upsert_key, created_at, updated_at, provenance_json, metadata_json, "
            "extra_json, current_revision_id FROM memories ORDER BY memory_id"
        ).fetchall()
        revision_rows = connection.execute(
            "SELECT revision_id, memory_id, content, content_sha256, created_at, created_by, metadata_json "
            "FROM memory_revisions ORDER BY revision_id"
        ).fetchall()
        outbox_rows = connection.execute(
            "SELECT event_id, aggregate_type, aggregate_id, event_type, event_version, payload_json, "
            "status, attempts, available_at, claimed_at, claim_token, last_error, created_at, updated_at "
            "FROM memory_outbox ORDER BY event_id"
        ).fetchall()
        link_rows = connection.execute(
            "SELECT link_id, project, source_id, target_id, relation, created_at, updated_at, metadata_json "
            "FROM memory_links ORDER BY link_id"
        ).fetchall()
        artifact_rows = connection.execute(
            "SELECT artifact_type, artifact_id, project, memory_id, lifecycle, created_at, updated_at, payload_json "
            "FROM memory_artifacts ORDER BY artifact_type, artifact_id"
        ).fetchall()
    if quick_check != "ok":
        raise MemoryClassMigrationError(f"SQLite quick_check failed: {quick_check}")
    if foreign_keys:
        raise MemoryClassMigrationError(
            f"SQLite foreign_key_check returned {len(foreign_keys)} rows"
        )
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
        "memory_columns": sorted(columns),
        "capability_version": str(capability_row[0]) if capability_row else None,
        "counts": counts,
        "authority_digest": hashlib.sha256(
            canonical_json([list(row) for row in authority_rows]).encode("utf-8")
        ).hexdigest(),
        "logical_digests": {
            "memories": hashlib.sha256(canonical_json([list(row) for row in memory_rows]).encode("utf-8")).hexdigest(),
            "revisions": hashlib.sha256(canonical_json([list(row) for row in revision_rows]).encode("utf-8")).hexdigest(),
            "outbox": hashlib.sha256(canonical_json([list(row) for row in outbox_rows]).encode("utf-8")).hexdigest(),
            "links": hashlib.sha256(canonical_json([list(row) for row in link_rows]).encode("utf-8")).hexdigest(),
            "artifacts": hashlib.sha256(canonical_json([list(row) for row in artifact_rows]).encode("utf-8")).hexdigest(),
        },
    }


def _manifest() -> dict[str, Any]:
    sql = ";\n".join(SQL_MANIFEST)
    return {
        "columns": list(TYPED_MEMORY_COLUMNS),
        "index": MEMORY_CLASS_INDEX,
        "indexes": sorted(TYPED_MEMORY_INDEXES),
        "capability_key": CAPABILITY_KEY,
        "capability_version": CAPABILITY_VERSION,
        "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        "preserve_user_version": FRESHNESS_SCHEMA_VERSION,
    }


def _connection_logical_digests(connection: sqlite3.Connection) -> dict[str, Any]:
    """Read authoritative invariants from an open connection.

    Migration validation must happen before ``COMMIT``.  A second read-only
    connection cannot observe the uncommitted DDL and data, so post-commit
    assertions would otherwise be able to raise after the database was already
    changed.  This helper deliberately includes only lifecycle-authoritative
    rows; the additive typed columns are validated separately as schema state.
    """

    counts = {
        "memories": int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]),
        "revisions": int(connection.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0]),
        "outbox": int(connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0]),
    }
    authority_rows = connection.execute(
        """
        SELECT m.memory_id, m.current_revision_id, r.content_sha256
        FROM memories AS m
        JOIN memory_revisions AS r
          ON r.revision_id = m.current_revision_id
        ORDER BY m.memory_id
        """
    ).fetchall()
    memory_rows = connection.execute(
        "SELECT memory_id, project, memory_type, lifecycle, title, summary, tags_json, files_json, "
        "session_refs_json, upsert_key, created_at, updated_at, provenance_json, metadata_json, "
        "extra_json, current_revision_id FROM memories ORDER BY memory_id"
    ).fetchall()
    revision_rows = connection.execute(
        "SELECT revision_id, memory_id, content, content_sha256, created_at, created_by, metadata_json "
        "FROM memory_revisions ORDER BY revision_id"
    ).fetchall()
    outbox_rows = connection.execute(
        "SELECT event_id, aggregate_type, aggregate_id, event_type, event_version, payload_json, "
        "status, attempts, available_at, claimed_at, claim_token, last_error, created_at, updated_at "
        "FROM memory_outbox ORDER BY event_id"
    ).fetchall()
    link_rows = connection.execute(
        "SELECT link_id, project, source_id, target_id, relation, created_at, updated_at, metadata_json "
        "FROM memory_links ORDER BY link_id"
    ).fetchall()
    artifact_rows = connection.execute(
        "SELECT artifact_type, artifact_id, project, memory_id, lifecycle, created_at, updated_at, payload_json "
        "FROM memory_artifacts ORDER BY artifact_type, artifact_id"
    ).fetchall()
    return {
        "counts": counts,
        "authority_digest": hashlib.sha256(
            canonical_json([list(row) for row in authority_rows]).encode("utf-8")
        ).hexdigest(),
        "logical_digests": {
            "memories": hashlib.sha256(
                canonical_json([list(row) for row in memory_rows]).encode("utf-8")
            ).hexdigest(),
            "revisions": hashlib.sha256(
                canonical_json([list(row) for row in revision_rows]).encode("utf-8")
            ).hexdigest(),
            "outbox": hashlib.sha256(
                canonical_json([list(row) for row in outbox_rows]).encode("utf-8")
            ).hexdigest(),
            "links": hashlib.sha256(
                canonical_json([list(row) for row in link_rows]).encode("utf-8")
            ).hexdigest(),
            "artifacts": hashlib.sha256(
                canonical_json([list(row) for row in artifact_rows]).encode("utf-8")
            ).hexdigest(),
        },
    }


def _connection_schema_state(connection: sqlite3.Connection) -> dict[str, Any]:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(memories)").fetchall()
    }
    indexes = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    marker = connection.execute(
        "SELECT value FROM memory_store_meta WHERE key = ?",
        (CAPABILITY_KEY,),
    ).fetchone()
    return {
        "memory_columns": columns,
        "indexes": indexes,
        "capability_version": str(marker[0]) if marker else None,
    }


def _validate_source_schema(fingerprint: Mapping[str, Any], *, allow_current: bool) -> None:
    if int(fingerprint.get("user_version") or -1) != FRESHNESS_SCHEMA_VERSION:
        raise MemoryClassMigrationError(
            f"expected SQLite user_version {FRESHNESS_SCHEMA_VERSION}, "
            f"found {fingerprint.get('user_version')}"
        )
    tables = set(fingerprint.get("tables") or [])
    if not FRESHNESS_SCHEMA_TABLES.issubset(tables):
        raise MemoryClassMigrationError("freshness v2 tables are missing")
    columns = set(fingerprint.get("memory_columns") or [])
    present = set(TYPED_MEMORY_COLUMNS) & columns
    current = (
        set(TYPED_MEMORY_COLUMNS).issubset(columns)
        and set(TYPED_MEMORY_INDEXES).issubset(set(fingerprint.get("indexes") or []))
        and fingerprint.get("capability_version") == CAPABILITY_VERSION
    )
    if current and allow_current:
        return
    if present:
        raise MemoryClassMigrationError(
            f"partial memory-class schema detected: {sorted(present)}"
        )
    if fingerprint.get("capability_version") is not None:
        raise MemoryClassMigrationError("memory-class capability marker exists without schema")


def build_migration_plan(
    database: str | Path,
    existing_backup: str | Path,
) -> dict[str, Any]:
    """Build a deterministic read-only plan bound to DB and backup digests."""

    database_path = assert_safe_path(database).resolve()
    backup_path = assert_safe_path(existing_backup).resolve()
    if database_path == backup_path:
        raise MemoryClassMigrationError("database and existing backup must be different files")
    database_fingerprint = _database_fingerprint(database_path)
    _validate_source_schema(database_fingerprint, allow_current=False)
    backup_fingerprint = _database_fingerprint(backup_path)
    if backup_fingerprint["counts"] != database_fingerprint["counts"]:
        raise MemoryClassMigrationError("existing backup does not match authoritative row counts")
    if backup_fingerprint["authority_digest"] != database_fingerprint["authority_digest"]:
        raise MemoryClassMigrationError("existing backup does not match authoritative content digest")
    if backup_fingerprint["logical_digests"] != database_fingerprint["logical_digests"]:
        raise MemoryClassMigrationError("existing backup does not match logical snapshot digests")
    plan = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "action": "add-memory-class-capability",
        "database": database_fingerprint,
        "existing_full_backup": backup_fingerprint,
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


def _verify_plan(
    plan: Mapping[str, Any],
    database: Path,
    backup: Path,
    expected_plan_digest: str,
) -> None:
    unsigned = dict(plan)
    supplied_digest = str(unsigned.pop("plan_digest", ""))
    actual_digest = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    if supplied_digest != expected_plan_digest or actual_digest != expected_plan_digest:
        raise MemoryClassMigrationError("migration plan digest mismatch")
    if plan.get("manifest") != _manifest():
        raise MemoryClassMigrationError("migration manifest changed since plan")
    current = _database_fingerprint(database)
    planned = plan.get("database") or {}
    for key in ("size", "mtime_ns", "sha256", "user_version", "counts"):
        if current.get(key) != planned.get(key):
            raise MemoryClassMigrationError(f"database changed since plan: {key}")
    current_backup = _database_fingerprint(backup)
    planned_backup = plan.get("existing_full_backup") or {}
    for key in (
        "size",
        "sha256",
        "quick_check",
        "foreign_key_errors",
        "counts",
        "authority_digest",
        "logical_digests",
    ):
        if current_backup.get(key) != planned_backup.get(key):
            raise MemoryClassMigrationError(f"existing backup changed since plan: {key}")


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
    """Apply the exact additive capability transaction after explicit gates."""

    if not confirm_operator:
        raise MemoryClassMigrationError("apply requires explicit operator confirmation")
    if not offline_verified:
        raise MemoryClassMigrationError("apply requires an offline writer/listener proof")
    database_path = assert_safe_path(database).resolve()
    backup_path = assert_safe_path(existing_backup).resolve()
    if database_path == backup_path:
        raise MemoryClassMigrationError("database and existing backup must be different files")
    _verify_plan(plan, database_path, backup_path, expected_plan_digest)
    before = _database_fingerprint(database_path)
    columns = set(before["memory_columns"])
    if (
        set(TYPED_MEMORY_COLUMNS).issubset(columns)
        and set(TYPED_MEMORY_INDEXES).issubset(set(before["indexes"]))
        and before["capability_version"] == CAPABILITY_VERSION
    ):
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "ok": True,
            "action": "already-current",
            "plan_digest": expected_plan_digest,
            "database": before,
            "execution": {"sqlite_written": False},
        }
    connection = sqlite3.connect(database_path, timeout=30.0, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in SQL_MANIFEST:
            connection.execute(statement)
        typed_values: dict[str, dict[str, Any]] = {}
        allowed = {
            "memory_class": {"episodic", "semantic", "procedural", "working", "unclassified"},
            "memory_class_source": {
                "legacy-default", "request-default", "caller-explicit", "deterministic-rule", "review-confirmed"
            },
            "event_role": {"fact", "decision", "qa", "trace", "feedback", "skill_run", "unclassified"},
        }
        for row in connection.execute("SELECT memory_id, metadata_json FROM memories ORDER BY memory_id"):
            try:
                metadata = json.loads(str(row[1]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise MemoryClassMigrationError(f"invalid metadata_json for {row[0]}") from exc
            if not isinstance(metadata, dict):
                raise MemoryClassMigrationError(f"metadata_json for {row[0]} is not an object")
            values: dict[str, Any] = {}
            for key, valueset in allowed.items():
                if key in metadata:
                    value = metadata[key]
                    if value not in valueset:
                        raise MemoryClassMigrationError(f"invalid {key} in metadata for {row[0]}")
                    values[key] = value
            if "memory_class_confidence" in metadata:
                value = metadata["memory_class_confidence"]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                    raise MemoryClassMigrationError(f"invalid memory_class_confidence in metadata for {row[0]}")
                values["memory_class_confidence"] = value
            if "event_role_version" in metadata and str(metadata["event_role_version"]) != "1":
                raise MemoryClassMigrationError(f"invalid event_role_version in metadata for {row[0]}")
            if values:
                typed_values[str(row[0])] = values
        for memory_id, values in typed_values.items():
            assignments = ", ".join(f"{key} = ?" for key in values)
            connection.execute(
                f"UPDATE memories SET {assignments} WHERE memory_id = ?",
                (*values.values(), memory_id),
            )
        connection.execute(
            "INSERT OR REPLACE INTO memory_store_meta(key, value) VALUES (?, ?)",
            (CAPABILITY_KEY, CAPABILITY_VERSION),
        )
        if inject_failure:
            raise MemoryClassMigrationError("injected migration failure")

        # Validate every invariant while the transaction is still open.  If a
        # check fails, the exception below rolls back the additive DDL and no
        # caller can observe a half-applied migration.
        schema_state = _connection_schema_state(connection)
        if not set(TYPED_MEMORY_COLUMNS).issubset(schema_state["memory_columns"]):
            raise MemoryClassMigrationError("migration did not create all typed memory columns")
        if not set(TYPED_MEMORY_INDEXES).issubset(schema_state["indexes"]):
            raise MemoryClassMigrationError("migration did not create all typed memory indexes")
        if schema_state["capability_version"] != CAPABILITY_VERSION:
            raise MemoryClassMigrationError("migration capability marker is missing or invalid")
        connection_state = _connection_logical_digests(connection)
        if connection_state["counts"] != before["counts"]:
            raise MemoryClassMigrationError("migration changed authoritative row counts")
        if connection_state["authority_digest"] != before["authority_digest"]:
            raise MemoryClassMigrationError("migration changed authoritative memory content")
        if connection_state["logical_digests"] != before["logical_digests"]:
            raise MemoryClassMigrationError("migration changed logical memories, revisions, or outbox")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    after = _database_fingerprint(database_path)
    # These are confirmation-only checks.  The same invariants were validated
    # before COMMIT above, so a failure here must not become a new mutation or
    # rollback path after the durable commit.  Preserve the diagnostic in the
    # receipt instead of raising from this phase.
    confirmation: dict[str, Any] = {"ok": True, "warnings": []}
    try:
        _validate_source_schema(after, allow_current=True)
        if int(after["user_version"]) != int(before["user_version"]):
            raise MemoryClassMigrationError("migration changed PRAGMA user_version")
        if after["counts"] != before["counts"]:
            raise MemoryClassMigrationError("migration changed authoritative row counts")
        if after["authority_digest"] != before["authority_digest"]:
            raise MemoryClassMigrationError("migration changed authoritative memory content")
        if after["logical_digests"] != before["logical_digests"]:
            raise MemoryClassMigrationError("migration changed logical memories, revisions, or outbox")
    except MemoryClassMigrationError as exc:
        confirmation = {"ok": False, "warnings": [str(exc)]}
    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
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
        "rollback": (
            "disable the memory-class feature while retaining additive columns; "
            "for schema rollback stop runtime and restore the verified backup offline"
        ),
    }


__all__ = [
    "CAPABILITY_KEY",
    "CAPABILITY_VERSION",
    "EVENT_ROLE_COLUMNS",
    "EVENT_ROLE_INDEX",
    "MEMORY_CLASS_COLUMNS",
    "MEMORY_CLASS_INDEX",
    "MIGRATION_SCHEMA_VERSION",
    "MemoryClassMigrationError",
    "SQL_MANIFEST",
    "TYPED_MEMORY_COLUMNS",
    "TYPED_MEMORY_INDEXES",
    "apply_migration",
    "build_migration_plan",
]
