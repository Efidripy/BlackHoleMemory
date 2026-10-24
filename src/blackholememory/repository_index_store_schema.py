"""Repository index store operations."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .filesystem_boundaries import assert_safe_path
from .repository_index_types import (
    REPOSITORY_INDEX_BUSY_TIMEOUT_MS,
    REPOSITORY_INDEX_STORE_SCHEMA_VERSION,
    REPOSITORY_INDEX_WRITE_RETRY_DELAYS,
    RepositoryIndexError,
    RepositoryIndexMigrationRequired,
    _REPOSITORY_INDEX_TABLES,
    _clip,
    _sha256_file,
    _utc_now,
)

_LOGGER = logging.getLogger(__name__)

class _RepositoryIndexStoreSchemaMixin:
    """Repository snapshot tables inside the canonical BHM SQLite database."""
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = REPOSITORY_INDEX_BUSY_TIMEOUT_MS) -> None:
        # Keep the lexical path intact until the filesystem boundary has been
        # admitted.  Resolving first would hide a symlink/junction component
        # and could turn a linked SQLite target into an apparently safe path.
        self.path = Path(path).expanduser()
        self.busy_timeout_ms = max(int(busy_timeout_ms), 100)
        self._initialize_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._initialized = False

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        assert_safe_path(self.path)
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.path.as_posix()}?mode=ro",
                uri=True,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
        else:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _begin_immediate(connection: sqlite3.Connection) -> None:
        for attempt, delay in enumerate((0.0, *REPOSITORY_INDEX_WRITE_RETRY_DELAYS)):
            if delay:
                time.sleep(delay)
            try:
                connection.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).casefold() or attempt >= len(REPOSITORY_INDEX_WRITE_RETRY_DELAYS):
                    raise

    def inspect_schema(self, *, fast: bool = False) -> dict[str, Any]:
        """Inspect repository-index tables without creating or mutating them.

        ``fast=True`` is reserved for hot read paths.  It validates only the
        schema shape/version and deliberately skips row counts and the full
        ``PRAGMA quick_check`` scan; operator health and acceptance callers
        retain the default integrity-complete behavior.
        """

        assert_safe_path(self.path)
        if not self.path.is_file():
            return {
                "database_exists": False,
                "database_path": str(self.path),
                "schema_version": None,
                "ready": False,
                "tables": [],
                "missing_tables": sorted(_REPOSITORY_INDEX_TABLES),
                "row_counts": {},
                "quick_check": None,
            }
        connection = self._connect(read_only=True)
        try:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            repository_tables = sorted(table for table in tables if table.startswith("repository_"))
            version: int | None = None
            if "repository_index_meta" in tables:
                row = connection.execute(
                    "SELECT value FROM repository_index_meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is not None:
                    version = int(row["value"])
            missing = sorted(_REPOSITORY_INDEX_TABLES - tables)
            counts = {} if fast else {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in repository_tables
            }
            quick_check = None if fast else str(connection.execute("PRAGMA quick_check").fetchone()[0])
            return {
                "database_exists": True,
                "database_path": str(self.path),
                "schema_version": version,
                "ready": version == REPOSITORY_INDEX_STORE_SCHEMA_VERSION and not missing and (fast or quick_check == "ok"),
                "tables": repository_tables,
                "missing_tables": missing,
                "row_counts": counts,
                "quick_check": quick_check,
            }
        finally:
            connection.close()

    @staticmethod
    def _watch_checkpoint_key(project: str, root_id: str) -> str:
        return f"watch_checkpoint:{_clip(project, 120)}:{_clip(root_id, 180)}"

    def get_watch_checkpoint(self, project: str, root_id: str) -> dict[str, Any] | None:
        """Read the last durable watcher checkpoint without changing state."""

        self.initialize()
        connection = self._connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT value FROM repository_index_meta WHERE key = ?",
                (self._watch_checkpoint_key(project, root_id),),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        try:
            value = json.loads(str(row["value"]))
        except (TypeError, json.JSONDecodeError):
            return None
        return dict(value) if isinstance(value, dict) else None

    def save_watch_checkpoint(self, project: str, root_id: str, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a bounded crash-recovery checkpoint in SQLite metadata."""

        self.initialize()
        payload = {
            "schema_version": "bhm.repository-watch-checkpoint.v1",
            "project": _clip(project, 120),
            "root_id": _clip(root_id, 180),
            **{str(key): value for key, value in checkpoint.items()},
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(serialized) > 16_384:
            raise RepositoryIndexError("watch checkpoint exceeds bounded metadata size")
        connection = self._connect()
        try:
            with self._write_lock:
                self._begin_immediate(connection)
                connection.execute(
                    "INSERT OR REPLACE INTO repository_index_meta(key, value) VALUES (?, ?)",
                    (self._watch_checkpoint_key(project, root_id), serialized),
                )
                connection.commit()
        except Exception:  # broad: preserve rollback for any transaction failure
            _LOGGER.error("repository index checkpoint transaction failed; rolling back")
            connection.rollback()
            raise
        finally:
            connection.close()
        return payload

    @staticmethod
    def _canonical_memory_counts(connection: sqlite3.Connection) -> dict[str, int]:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        return {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in ("memories", "memory_revisions", "memory_outbox", "memory_links", "memory_artifacts")
            if table in tables
        }

    def migrate_empty_v1_to_v2(self, backup_path: str | Path) -> dict[str, Any]:
        """Back up and replace only an empty WI-01 v1 schema with v2."""

        status = self.inspect_schema()
        if status["schema_version"] == REPOSITORY_INDEX_STORE_SCHEMA_VERSION and status["ready"]:
            return {
                "schema_version": "bhm.repository-index-migration.v1",
                "ok": True,
                "action": "already-current",
                "database_path": str(self.path),
                "backup_path": None,
                "writes_sqlite_state": False,
            }
        if status["schema_version"] != 1:
            raise RepositoryIndexMigrationRequired(
                f"expected repository index schema 1, found {status['schema_version']!r}"
            )
        non_empty = {
            table: count
            for table, count in status["row_counts"].items()
            if table != "repository_index_meta" and int(count) > 0
        }
        if non_empty:
            raise RepositoryIndexMigrationRequired(
                f"v1 repository index contains data and requires a dedicated migration: {non_empty}"
            )
        backup = Path(backup_path).expanduser()
        assert_safe_path(backup)
        if backup.exists():
            raise RepositoryIndexError(f"backup path already exists: {backup}")
        assert_safe_path(backup)
        backup.parent.mkdir(parents=True, exist_ok=True)
        assert_safe_path(backup.parent, reject_hardlink_target=False)
        assert_safe_path(backup)
        source_connection = self._connect()
        try:
            destination = sqlite3.connect(
                backup,
                timeout=self.busy_timeout_ms / 1000,
            )
            destination.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            try:
                source_connection.backup(destination)
                destination.commit()
            finally:
                destination.close()
            backup_connection = sqlite3.connect(
                f"file:{backup.as_posix()}?mode=ro",
                uri=True,
                timeout=self.busy_timeout_ms / 1000,
            )
            backup_connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            try:
                backup_quick_check = str(backup_connection.execute("PRAGMA quick_check").fetchone()[0])
            finally:
                backup_connection.close()
            if backup_quick_check != "ok":
                raise RepositoryIndexError(f"SQLite backup quick_check failed: {backup_quick_check}")
            backup_sha256 = _sha256_file(backup)
            before_memory_counts = self._canonical_memory_counts(source_connection)
            try:
                self._begin_immediate(source_connection)
                for table in (
                    "repository_index_current",
                    "repository_source_imports",
                    "repository_index_snapshot_skips",
                    "repository_index_snapshot_files",
                    "repository_index_snapshots",
                    "repository_index_job_skips",
                    "repository_index_job_files",
                    "repository_index_job_candidates",
                    "repository_index_jobs",
                    "repository_index_meta",
                ):
                    source_connection.execute(f'DROP TABLE IF EXISTS "{table}"')
                source_connection.commit()
            except Exception:  # broad: preserve rollback for any migration transaction failure
                _LOGGER.error("repository index migration transaction failed; rolling back")
                source_connection.rollback()
                raise
        finally:
            source_connection.close()
        self._initialized = False
        self.initialize()
        connection = self._transaction()
        try:
            connection.executemany(
                "INSERT OR REPLACE INTO repository_index_meta(key, value) VALUES (?, ?)",
                (
                    ("migrated_from", "1"),
                    ("migrated_at", _utc_now()),
                    ("migration_backup_path", str(backup)),
                    ("migration_backup_sha256", backup_sha256),
                ),
            )
            after_memory_counts = self._canonical_memory_counts(connection)
            if after_memory_counts != before_memory_counts:
                raise RepositoryIndexError("canonical memory row counts changed during repository-index migration")
            connection.commit()
        except Exception:  # broad: preserve rollback for any migration metadata failure
            _LOGGER.error("repository index migration metadata failed; rolling back")
            connection.rollback()
            raise
        finally:
            connection.close()
        final_status = self.inspect_schema()
        if not final_status["ready"]:
            raise RepositoryIndexError(f"repository index migration did not reach ready state: {final_status}")
        return {
            "schema_version": "bhm.repository-index-migration.v1",
            "ok": True,
            "action": "migrated-empty-v1-to-v2",
            "database_path": str(self.path),
            "backup_path": str(backup),
            "backup_sha256": backup_sha256,
            "backup_quick_check": backup_quick_check,
            "memory_counts_before": before_memory_counts,
            "memory_counts_after": after_memory_counts,
            "repository_schema": final_status,
            "writes_sqlite_state": True,
            "rollback": "stop the authoritative runtime and restore the hash-verified SQLite backup",
        }

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            assert_safe_path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            assert_safe_path(self.path.parent, reject_hardlink_target=False)
            assert_safe_path(self.path)
            existing = self.inspect_schema()
            if existing["schema_version"] not in {None, REPOSITORY_INDEX_STORE_SCHEMA_VERSION}:
                raise RepositoryIndexMigrationRequired(
                    f"repository index schema {existing['schema_version']} requires explicit migration "
                    f"to {REPOSITORY_INDEX_STORE_SCHEMA_VERSION}"
                )
            if existing["schema_version"] is None and existing["tables"]:
                raise RepositoryIndexMigrationRequired(
                    f"repository index tables exist without schema metadata: {existing['tables']}"
                )
            connection = self._connect()
            try:
                mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold()
                if mode != "wal":
                    raise RepositoryIndexError(f"SQLite refused WAL mode for {self.path}: {mode}")
                self._begin_immediate(connection)
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS repository_index_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS repository_index_jobs (
                        job_id TEXT PRIMARY KEY,
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        root_path TEXT NOT NULL,
                        state_digest TEXT NOT NULL,
                        config_digest TEXT NOT NULL,
                        candidate_digest TEXT NOT NULL,
                        git_status_sha256 TEXT NOT NULL,
                        git_head TEXT,
                        git_branch TEXT,
                        dirty INTEGER NOT NULL CHECK (dirty IN (0, 1)),
                        source_json TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
                        total_candidates INTEGER NOT NULL CHECK (total_candidates >= 0),
                        prefiltered_skip_count INTEGER NOT NULL CHECK (prefiltered_skip_count >= 0),
                        processed_count INTEGER NOT NULL DEFAULT 0 CHECK (processed_count >= 0),
                        indexed_count INTEGER NOT NULL DEFAULT 0 CHECK (indexed_count >= 0),
                        skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
                        reused_count INTEGER NOT NULL DEFAULT 0 CHECK (reused_count >= 0),
                        cursor_path TEXT,
                        snapshot_id TEXT,
                        started_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT,
                        error_code TEXT,
                        error_detail TEXT,
                        UNIQUE(project, root_id, state_digest, config_digest, source_json)
                    );

                    CREATE INDEX IF NOT EXISTS idx_repository_index_jobs_scope_status
                        ON repository_index_jobs(project, root_id, status, updated_at DESC, job_id);

                    CREATE TABLE IF NOT EXISTS repository_index_job_candidates (
                        job_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                        mtime_ns INTEGER NOT NULL,
                        origin TEXT NOT NULL,
                        PRIMARY KEY (job_id, path),
                        FOREIGN KEY (job_id) REFERENCES repository_index_jobs(job_id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS repository_index_job_files (
                        job_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                        line_count INTEGER NOT NULL CHECK (line_count >= 0),
                        mtime_ns INTEGER NOT NULL,
                        language TEXT NOT NULL,
                        file_kind TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        PRIMARY KEY (job_id, path),
                        FOREIGN KEY (job_id) REFERENCES repository_index_jobs(job_id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS repository_index_job_skips (
                        job_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
                        PRIMARY KEY (job_id, path),
                        FOREIGN KEY (job_id) REFERENCES repository_index_jobs(job_id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS repository_index_snapshots (
                        snapshot_id TEXT PRIMARY KEY,
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        root_path TEXT NOT NULL,
                        state_digest TEXT NOT NULL,
                        snapshot_digest TEXT NOT NULL UNIQUE,
                        graph_input_digest TEXT NOT NULL,
                        config_digest TEXT NOT NULL,
                        candidate_digest TEXT NOT NULL,
                        git_status_sha256 TEXT NOT NULL,
                        git_head TEXT,
                        git_branch TEXT,
                        dirty INTEGER NOT NULL CHECK (dirty IN (0, 1)),
                        previous_snapshot_id TEXT,
                        source_json TEXT NOT NULL,
                        summary_json TEXT NOT NULL,
                        delta_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        FOREIGN KEY (previous_snapshot_id)
                            REFERENCES repository_index_snapshots(snapshot_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_repository_index_snapshots_scope_time
                        ON repository_index_snapshots(project, root_id, completed_at DESC, snapshot_id);

                    CREATE TABLE IF NOT EXISTS repository_index_snapshot_files (
                        snapshot_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                        line_count INTEGER NOT NULL CHECK (line_count >= 0),
                        mtime_ns INTEGER NOT NULL,
                        language TEXT NOT NULL,
                        file_kind TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        PRIMARY KEY (snapshot_id, path),
                        FOREIGN KEY (snapshot_id)
                            REFERENCES repository_index_snapshots(snapshot_id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_repository_snapshot_files_hash
                        ON repository_index_snapshot_files(content_sha256, snapshot_id, path);

                    CREATE TABLE IF NOT EXISTS repository_index_snapshot_skips (
                        snapshot_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
                        PRIMARY KEY (snapshot_id, path),
                        FOREIGN KEY (snapshot_id)
                            REFERENCES repository_index_snapshots(snapshot_id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS repository_source_imports (
                        source_import_id TEXT PRIMARY KEY,
                        snapshot_id TEXT NOT NULL UNIQUE,
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        revision TEXT NOT NULL,
                        license TEXT NOT NULL,
                        evidence_class TEXT NOT NULL,
                        owner TEXT NOT NULL,
                        source_registry_id TEXT,
                        extraction_version TEXT NOT NULL,
                        extracted_at TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        provenance_json TEXT NOT NULL,
                        FOREIGN KEY (snapshot_id)
                            REFERENCES repository_index_snapshots(snapshot_id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_repository_source_imports_scope
                        ON repository_source_imports(project, root_id, extracted_at DESC);

                    CREATE TABLE IF NOT EXISTS repository_index_current (
                        project TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        snapshot_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (project, root_id),
                        FOREIGN KEY (snapshot_id)
                            REFERENCES repository_index_snapshots(snapshot_id)
                    );
                    """
                )
                stored = connection.execute(
                    "SELECT value FROM repository_index_meta WHERE key = 'schema_version'"
                ).fetchone()
                if stored is not None and int(stored["value"]) != REPOSITORY_INDEX_STORE_SCHEMA_VERSION:
                    raise RepositoryIndexError(
                        f"unsupported repository index schema {stored['value']}; "
                        f"expected {REPOSITORY_INDEX_STORE_SCHEMA_VERSION}"
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO repository_index_meta(key, value) VALUES ('schema_version', ?)",
                    (str(REPOSITORY_INDEX_STORE_SCHEMA_VERSION),),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO repository_index_meta(key, value) VALUES ('created_at', ?)",
                    (_utc_now(),),
                )
                connection.execute("PRAGMA wal_autocheckpoint=1000")
                connection.commit()
                self._initialized = True
            except Exception:  # broad: preserve rollback for any schema transaction failure
                _LOGGER.error("repository index schema transaction failed; rolling back")
                connection.rollback()
                raise
            finally:
                connection.close()



