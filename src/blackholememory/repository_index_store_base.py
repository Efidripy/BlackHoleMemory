"""SQLite store base: schema initialization, migrations, and core job and snapshot persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .filesystem_boundaries import assert_safe_path
from .repository_index_snapshots import (
    _build_delta,
    _build_snapshot_summary,
    _snapshot_digest_files,
)
from .repository_index_types import (
    REPOSITORY_INDEX_BUSY_TIMEOUT_MS,
    REPOSITORY_INDEX_SCHEMA_VERSION,
    REPOSITORY_INDEX_STORE_SCHEMA_VERSION,
    REPOSITORY_INDEX_WRITE_RETRY_DELAYS,
    RepositoryIndexError,
    RepositoryIndexMigrationRequired,
    RepositorySkip,
    RepositorySourceProvenance,
    RepositoryState,
    _REPOSITORY_INDEX_TABLES,
    _canonical_json,
    _clip,
    _sha256_file,
    _sha256_json,
    _utc_now,
    repository_index_error_retryable,
)

_LOGGER = logging.getLogger(__name__)


class _RepositoryIndexStoreBase:
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

    def _transaction(self) -> sqlite3.Connection:
        self.initialize()
        connection = self._connect()
        self._begin_immediate(connection)
        return connection

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def begin_or_resume_job(
        self,
        state: RepositoryState,
        source: RepositorySourceProvenance,
        *,
        started_at: str | None = None,
        force_refresh_nonce: str | None = None,
    ) -> dict[str, Any]:
        source_payload = source.as_dict()
        if force_refresh_nonce:
            # A forced refresh is an explicit operator epoch.  Binding the
            # nonce into the source digest makes the resulting snapshot
            # immutable and freshness-visible without changing file content.
            source_payload["refresh_nonce"] = str(force_refresh_nonce)
        source_json = _canonical_json(source_payload)
        job_key = {
            "project": state.project,
            "root_id": state.root_id,
            "state_digest": state.state_digest,
            "config_digest": state.config_digest,
            "source": source_payload,
        }
        job_id = f"job_bhm_{_sha256_json(job_key)[:24]}"
        now = started_at or _utc_now()
        with self._write_lock:
            connection = self._transaction()
            try:
                row = connection.execute(
                    "SELECT * FROM repository_index_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if not force_refresh_nonce and (row is None or row["status"] == "completed"):
                    # A bounded forced refresh may have been interrupted after
                    # one batch.  Resume that running epoch instead of creating
                    # a fresh job that can accidentally republish its prior
                    # snapshot and digest.
                    running_row = connection.execute(
                        """
                        SELECT * FROM repository_index_jobs
                        WHERE project = ? AND root_id = ? AND state_digest = ?
                          AND config_digest = ? AND status = 'running'
                        ORDER BY updated_at DESC, job_id
                        LIMIT 1
                        """,
                        (state.project, state.root_id, state.state_digest, state.config_digest),
                    ).fetchone()
                    if running_row is not None:
                        row = running_row
                        job_id = str(row["job_id"])
                    else:
                        # A forced refresh creates a distinct immutable job for
                        # the same repository state.  Prefer the newest
                        # completed epoch so a later non-force call cannot
                        # republish the older deterministic job and move the
                        # authoritative current pointer backwards.
                        completed_row = connection.execute(
                            """
                            SELECT * FROM repository_index_jobs
                            WHERE project = ? AND root_id = ? AND state_digest = ?
                              AND config_digest = ? AND status = 'completed'
                            ORDER BY completed_at DESC, updated_at DESC, job_id DESC
                            LIMIT 1
                            """,
                            (state.project, state.root_id, state.state_digest, state.config_digest),
                        ).fetchone()
                        row = completed_row
                        if row is not None:
                            job_id = str(row["job_id"])
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO repository_index_jobs(
                            job_id, project, root_id, root_path, state_digest,
                            config_digest, candidate_digest, git_status_sha256, git_head, git_branch,
                            dirty, source_json, status, total_candidates,
                            prefiltered_skip_count, started_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            state.project,
                            state.root_id,
                            str(state.root),
                            state.state_digest,
                            state.config_digest,
                            state.candidate_digest,
                            state.git_status_sha256,
                            state.git_head,
                            state.git_branch,
                            int(state.dirty),
                            source_json,
                            len(state.candidates),
                            len(state.prefiltered_skips),
                            now,
                            now,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO repository_index_job_candidates(
                            job_id, path, size_bytes, mtime_ns, origin
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            (job_id, item.path, item.size_bytes, item.mtime_ns, item.origin)
                            for item in state.candidates
                        ],
                    )
                    connection.executemany(
                        """
                        INSERT INTO repository_index_job_skips(job_id, path, reason, size_bytes)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(job_id, item.path, item.reason, item.size_bytes) for item in state.prefiltered_skips],
                    )
                elif row["status"] != "completed":
                    connection.execute(
                        """
                        UPDATE repository_index_jobs
                        SET status = 'running', updated_at = ?, error_code = NULL, error_detail = NULL
                        WHERE job_id = ?
                        """,
                        (now, job_id),
                    )
                connection.commit()
            except Exception:  # broad: preserve rollback for any job transaction failure
                _LOGGER.error("repository index job transaction failed; rolling back")
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.job(job_id)

    def job(self, job_id: str) -> dict[str, Any]:
        self.initialize()
        connection = self._connect(read_only=True)
        try:
            row = connection.execute("SELECT * FROM repository_index_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise RepositoryIndexError(f"repository index job not found: {job_id}")
            return dict(row)
        finally:
            connection.close()

    def running_jobs(
        self,
        project: str,
        root_id: str,
        *,
        limit: int = 16,
    ) -> list[dict[str, Any]]:
        """Read running index jobs without creating or mutating the store.

        Watcher backpressure is deliberately operator-managed.  This probe is
        fail-closed for an absent/unready index database and returns bounded
        metadata only; it never opens a write transaction or starts a worker.
        """

        if not 1 <= int(limit) <= 64:
            raise ValueError("limit must be between 1 and 64")
        if not self.path.is_file():
            return []
        schema = self.inspect_schema()
        if not schema["ready"]:
            return []
        connection = self._connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT job_id, project, root_id, state_digest, status,
                       total_candidates, processed_count, updated_at
                FROM repository_index_jobs
                WHERE project = ? AND root_id = ? AND status = 'running'
                ORDER BY updated_at DESC, job_id
                LIMIT ?
                """,
                (_clip(project, 120), _clip(root_id, 180), int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def processed_paths(self, job_id: str) -> set[str]:
        self.initialize()
        connection = self._connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT path FROM repository_index_job_files WHERE job_id = ?
                UNION
                SELECT path FROM repository_index_job_skips WHERE job_id = ?
                """,
                (job_id, job_id),
            ).fetchall()
            return {str(row["path"]) for row in rows}
        finally:
            connection.close()

    def save_batch(
        self,
        job_id: str,
        files: Sequence[Mapping[str, Any]],
        skips: Sequence[RepositorySkip],
        *,
        cursor_path: str | None,
        reused_count_delta: int = 0,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        now = updated_at or _utc_now()
        with self._write_lock:
            connection = self._transaction()
            try:
                row = connection.execute(
                    "SELECT status FROM repository_index_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if row is None or row["status"] != "running":
                    raise RepositoryIndexError(f"job is not writable: {job_id}")
                if files:
                    connection.executemany(
                        """
                        INSERT OR REPLACE INTO repository_index_job_files(
                            job_id, path, content_sha256, size_bytes, line_count,
                            mtime_ns, language, file_kind, origin
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                job_id,
                                str(item["path"]),
                                str(item["content_sha256"]),
                                int(item["size_bytes"]),
                                int(item["line_count"]),
                                int(item["mtime_ns"]),
                                str(item["language"]),
                                str(item["file_kind"]),
                                str(item["origin"]),
                            )
                            for item in files
                        ],
                    )
                if skips:
                    connection.executemany(
                        """
                        INSERT OR REPLACE INTO repository_index_job_skips(job_id, path, reason, size_bytes)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(job_id, item.path, item.reason, item.size_bytes) for item in skips],
                    )
                indexed_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM repository_index_job_files WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()[0]
                )
                skipped_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM repository_index_job_skips WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()[0]
                )
                processed_count = indexed_count + max(
                    0,
                    skipped_count
                    - int(
                        connection.execute(
                            "SELECT prefiltered_skip_count FROM repository_index_jobs WHERE job_id = ?",
                            (job_id,),
                        ).fetchone()[0]
                    ),
                )
                existing_reused = int(
                    connection.execute(
                        "SELECT reused_count FROM repository_index_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    """
                    UPDATE repository_index_jobs
                    SET processed_count = ?, indexed_count = ?, skipped_count = ?,
                        reused_count = ?, cursor_path = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        processed_count,
                        indexed_count,
                        skipped_count,
                        existing_reused + max(int(reused_count_delta), 0),
                        cursor_path,
                        now,
                        job_id,
                    ),
                )
                connection.commit()
            except Exception:  # broad: preserve rollback for any batch transaction failure
                _LOGGER.error("repository index batch transaction failed; rolling back")
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.job(job_id)

    def mark_failed(self, job_id: str, *, code: str, detail: str) -> dict[str, Any]:
        with self._write_lock:
            connection = self._transaction()
            try:
                connection.execute(
                    """
                    UPDATE repository_index_jobs
                    SET status = 'failed', error_code = ?, error_detail = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (_clip(code, 120), _clip(detail, 1_000), _utc_now(), job_id),
                )
                connection.commit()
            except Exception:  # broad: preserve rollback for any failure-state transaction
                _LOGGER.error("repository index failure-state transaction failed; rolling back")
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.job(job_id)

    def current_snapshot(self, project: str, root_id: str, *, include_files: bool = False) -> dict[str, Any] | None:
        schema = self.inspect_schema(fast=True)
        if not schema["ready"]:
            return None
        connection = self._connect(read_only=True)
        try:
            row = connection.execute(
                """
                SELECT snapshots.*
                FROM repository_index_current AS current
                JOIN repository_index_snapshots AS snapshots
                  ON snapshots.snapshot_id = current.snapshot_id
                WHERE current.project = ? AND current.root_id = ?
                """,
                (project, root_id),
            ).fetchone()
            if row is None:
                return None
            return self._snapshot_payload(connection, row, include_files=include_files)
        finally:
            connection.close()

    def promote_snapshot(
        self,
        snapshot_id: str,
        *,
        project: str,
        root_id: str,
        updated_at: str | None = None,
    ) -> None:
        """Repair the authoritative current pointer to an existing snapshot.

        A completed, content-addressed job may be safely reused after another
        operator moved the current pointer (for example, a stale cold-start
        watcher snapshot).  Re-publishing the already verified snapshot keeps
        the operation idempotent and does not re-read source or create a second
        snapshot.
        """
        snapshot = self.snapshot(snapshot_id, include_files=False)
        if str(snapshot.get("project")) != project or str(snapshot.get("root_id")) != root_id:
            raise RepositoryIndexError("snapshot ownership mismatch during pointer repair")
        now = updated_at or _utc_now()
        with self._write_lock:
            connection = self._transaction()
            try:
                connection.execute(
                    """
                    INSERT INTO repository_index_current(project, root_id, snapshot_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project, root_id) DO UPDATE SET
                        snapshot_id = excluded.snapshot_id,
                        updated_at = excluded.updated_at
                    """,
                    (project, root_id, snapshot_id, now),
                )
                connection.commit()
            except Exception:  # broad: preserve rollback for atomic pointer repair
                _LOGGER.error("repository index pointer-repair transaction failed; rolling back")
                connection.rollback()
                raise
            finally:
                connection.close()

    def snapshot(self, snapshot_id: str, *, include_files: bool = False, read_only: bool = False) -> dict[str, Any]:
        if read_only:
            schema = self.inspect_schema(fast=True)
            if not schema["ready"]:
                raise RepositoryIndexError("repository index schema is not ready")
        else:
            self.initialize()
        connection = self._connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT * FROM repository_index_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise RepositoryIndexError(f"repository snapshot not found: {snapshot_id}")
            return self._snapshot_payload(connection, row, include_files=include_files)
        finally:
            connection.close()

    @staticmethod
    def _snapshot_payload(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_files: bool,
    ) -> dict[str, Any]:
        payload = dict(row)
        payload["dirty"] = bool(payload["dirty"])
        payload["source"] = json.loads(str(payload.pop("source_json")))
        payload["summary"] = json.loads(str(payload.pop("summary_json")))
        payload["delta"] = json.loads(str(payload.pop("delta_json")))
        if include_files:
            payload["files"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT * FROM repository_index_snapshot_files WHERE snapshot_id = ? ORDER BY path",
                    (row["snapshot_id"],),
                ).fetchall()
            ]
            payload["skips"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT path, reason, size_bytes FROM repository_index_snapshot_skips "
                    "WHERE snapshot_id = ? ORDER BY path, reason",
                    (row["snapshot_id"],),
                ).fetchall()
            ]
        return payload

    def _job_material(self, connection: sqlite3.Connection, job_id: str) -> tuple[sqlite3.Row, list[dict[str, Any]], list[dict[str, Any]]]:
        job = connection.execute("SELECT * FROM repository_index_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if job is None:
            raise RepositoryIndexError(f"repository index job not found: {job_id}")
        files = [
            dict(row)
            for row in connection.execute(
                "SELECT path, content_sha256, size_bytes, line_count, mtime_ns, language, file_kind, origin "
                "FROM repository_index_job_files WHERE job_id = ? ORDER BY path",
                (job_id,),
            ).fetchall()
        ]
        skips = [
            dict(row)
            for row in connection.execute(
                "SELECT path, reason, size_bytes FROM repository_index_job_skips WHERE job_id = ? ORDER BY path, reason",
                (job_id,),
            ).fetchall()
        ]
        return job, files, skips

    def finalize_job(
        self,
        job_id: str,
        *,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        now = completed_at or _utc_now()
        with self._write_lock:
            connection = self._transaction()
            try:
                job, files, skips = self._job_material(connection, job_id)
                if job["status"] == "completed" and job["snapshot_id"]:
                    connection.commit()
                    return self.snapshot(str(job["snapshot_id"]), include_files=False)
                if int(job["processed_count"]) != int(job["total_candidates"]):
                    raise RepositoryIndexError(
                        f"job is incomplete: {job['processed_count']}/{job['total_candidates']}"
                    )
                current = connection.execute(
                    "SELECT snapshot_id FROM repository_index_current WHERE project = ? AND root_id = ?",
                    (job["project"], job["root_id"]),
                ).fetchone()
                previous_snapshot_id = str(current["snapshot_id"]) if current is not None else None
                previous_files: list[dict[str, Any]] = []
                if previous_snapshot_id:
                    previous_files = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT path, content_sha256, size_bytes, line_count, mtime_ns, language, file_kind, origin "
                            "FROM repository_index_snapshot_files WHERE snapshot_id = ? ORDER BY path",
                            (previous_snapshot_id,),
                        ).fetchall()
                    ]
                delta = _build_delta(previous_files, files)
                summary = _build_snapshot_summary(files, skips)
                source = json.loads(str(job["source_json"]))
                graph_input_digest = _sha256_json(
                    [
                        {
                            "path": item["path"],
                            "content_sha256": item["content_sha256"],
                            "language": item["language"],
                            "file_kind": item["file_kind"],
                        }
                        for item in files
                    ]
                )
                snapshot_core = {
                    "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
                    "project": job["project"],
                    "root_id": job["root_id"],
                    "config_digest": job["config_digest"],
                    "git_head": job["git_head"],
                    "git_branch": job["git_branch"],
                    "dirty": bool(job["dirty"]),
                    "git_status_sha256": job["git_status_sha256"],
                    "state_digest": job["state_digest"],
                    "candidate_digest": job["candidate_digest"],
                    "source": source,
                    "files": _snapshot_digest_files(files),
                    "skips": skips,
                }
                snapshot_digest = _sha256_json(snapshot_core)
                snapshot_id = f"snapshot_bhm_{snapshot_digest[:24]}"
                existing = connection.execute(
                    "SELECT snapshot_digest FROM repository_index_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
                if existing is not None and str(existing["snapshot_digest"]) != snapshot_digest:
                    raise RepositoryIndexError(f"snapshot id collision: {snapshot_id}")
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO repository_index_snapshots(
                            snapshot_id, project, root_id, root_path, state_digest,
                            snapshot_digest, graph_input_digest, config_digest,
                            candidate_digest, git_status_sha256, git_head, git_branch,
                            dirty, previous_snapshot_id,
                            source_json, summary_json, delta_json, created_at, completed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_id,
                            job["project"],
                            job["root_id"],
                            job["root_path"],
                            job["state_digest"],
                            snapshot_digest,
                            graph_input_digest,
                            job["config_digest"],
                            job["candidate_digest"],
                            job["git_status_sha256"],
                            job["git_head"],
                            job["git_branch"],
                            job["dirty"],
                            previous_snapshot_id if previous_snapshot_id != snapshot_id else None,
                            job["source_json"],
                            _canonical_json(summary),
                            _canonical_json(delta),
                            job["started_at"],
                            now,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO repository_index_snapshot_files(
                            snapshot_id, path, content_sha256, size_bytes,
                            line_count, mtime_ns, language, file_kind, origin
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                snapshot_id,
                                item["path"],
                                item["content_sha256"],
                                item["size_bytes"],
                                item["line_count"],
                                item["mtime_ns"],
                                item["language"],
                                item["file_kind"],
                                item["origin"],
                            )
                            for item in files
                        ],
                    )
                    connection.executemany(
                        """
                        INSERT INTO repository_index_snapshot_skips(snapshot_id, path, reason, size_bytes)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(snapshot_id, item["path"], item["reason"], item["size_bytes"]) for item in skips],
                    )
                    revision = str(job["git_head"] or f"dirty:{snapshot_digest}")
                    source_import_id = f"source_import_bhm_{_sha256_json({'snapshot_id': snapshot_id, 'source': source})[:24]}"
                    connection.execute(
                        """
                        INSERT INTO repository_source_imports(
                            source_import_id, snapshot_id, project, root_id,
                            source_url, revision, license, evidence_class, owner,
                            source_registry_id, extraction_version, extracted_at,
                            content_sha256, provenance_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source_import_id,
                            snapshot_id,
                            job["project"],
                            job["root_id"],
                            source["source_url"],
                            revision,
                            source["license"],
                            source["evidence_class"],
                            source["owner"],
                            source.get("source_registry_id"),
                            REPOSITORY_INDEX_SCHEMA_VERSION,
                            now,
                            snapshot_digest,
                            _canonical_json(
                                {
                                    "git_head": job["git_head"],
                                    "git_branch": job["git_branch"],
                                    "dirty": bool(job["dirty"]),
                                    "state_digest": job["state_digest"],
                                    "config_digest": job["config_digest"],
                                }
                            ),
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO repository_index_current(project, root_id, snapshot_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project, root_id) DO UPDATE SET
                        snapshot_id = excluded.snapshot_id,
                        updated_at = excluded.updated_at
                    """,
                    (job["project"], job["root_id"], snapshot_id, now),
                )
                connection.execute(
                    """
                    UPDATE repository_index_jobs
                    SET status = 'completed', snapshot_id = ?, completed_at = ?,
                        updated_at = ?, error_code = NULL, error_detail = NULL
                    WHERE job_id = ?
                    """,
                    (snapshot_id, now, now, job_id),
                )
                connection.commit()
            except Exception:  # broad: preserve rollback for atomic snapshot publication
                _LOGGER.error("repository index snapshot-publication transaction failed; rolling back")
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.snapshot(snapshot_id, include_files=False)

    def status(self, project: str, root_id: str, *, fast_schema: bool = False) -> dict[str, Any]:
        schema = self.inspect_schema(fast=fast_schema)
        if not schema["ready"]:
            return {
                "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
                "database_path": str(self.path),
                "project": project,
                "root_id": root_id,
                "repository_schema": schema,
                "current_snapshot": None,
                "latest_job": None,
                "retryable": False,
            }
        current = self.current_snapshot(project, root_id, include_files=False)
        connection = self._connect(read_only=True)
        try:
            job = connection.execute(
                """
                SELECT * FROM repository_index_jobs
                WHERE project = ? AND root_id = ?
                ORDER BY updated_at DESC, job_id DESC LIMIT 1
                """,
                (project, root_id),
            ).fetchone()
            latest_job = dict(job) if job is not None else None
            retryable = False
            if latest_job is not None and latest_job.get("status") == "failed":
                retryable = repository_index_error_retryable(
                    RepositoryIndexError(str(latest_job.get("error_detail") or latest_job.get("error_code") or ""))
                )
            return {
                "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
                "database_path": str(self.path),
                "project": project,
                "root_id": root_id,
                "repository_schema": schema,
                "current_snapshot": current,
                "latest_job": latest_job,
                "retryable": retryable,
            }
        finally:
            connection.close()



RepositoryIndexStoreBase = _RepositoryIndexStoreBase
