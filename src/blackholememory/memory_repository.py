"""Transactional SQLite repository for the Phase 2 memory aggregate.

The repository owns durable canonical rows and appends their domain events in
the same SQLite transaction. Mem0/Qdrant projections remain separate consumers
of the outbox and are not touched by this adapter.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .filesystem_boundaries import assert_safe_path
from .memory_repository_base import FRESHNESS_SCHEMA_TABLES  # noqa: F401
from .memory_repository_base import FRESHNESS_SCHEMA_VERSION  # noqa: F401
from .memory_repository_base import GOVERNED_CONSOLIDATION_SCHEMA_TABLES  # noqa: F401
from .memory_repository_base import GOVERNED_CONSOLIDATION_SCHEMA_VERSION  # noqa: F401
from .memory_repository_base import MEMORY_STORE_BUSY_TIMEOUT_MS  # noqa: F401
from .memory_repository_base import MEMORY_STORE_SCHEMA_LATEST_VERSION  # noqa: F401
from .memory_repository_base import MEMORY_STORE_SCHEMA_VERSION  # noqa: F401
from .memory_repository_base import MEMORY_STORE_WRITE_RETRY_DELAYS  # noqa: F401
from .memory_repository_base import MemoryNotFoundError  # noqa: F401
from .memory_repository_base import MemoryRepository  # noqa: F401
from .memory_repository_base import MemoryRepositoryError  # noqa: F401
from .memory_repository_base import MemoryRepositoryIntegrityError  # noqa: F401
from .memory_repository_base import MemoryRevisionConflict  # noqa: F401
from .memory_repository_base import PROJECTION_GENERATION_BINDING_SCHEMA_TABLES  # noqa: F401
from .memory_repository_base import PROJECTION_GENERATION_BINDING_SCHEMA_VERSION  # noqa: F401
from .memory_repository_base import RepositoryHealth  # noqa: F401
from .memory_repository_base import SUPPORTED_MEMORY_STORE_SCHEMA_VERSIONS  # noqa: F401
from .memory_repository_base import SaveMemoryResult  # noqa: F401
from .memory_repository_base import _REQUIRED_MEMORY_STORE_TABLES  # noqa: F401
from .memory_repository_base import _bounded_page  # noqa: F401
from .memory_repository_base import _json_dumps  # noqa: F401
from .memory_repository_base import _json_loads  # noqa: F401
from .memory_repository_base import _memory_event_id  # noqa: F401
from .memory_repository_base import _memory_event_type  # noqa: F401
from .memory_repository_read import MemoryRepositoryReadMixin  # noqa: F401
from .memory_repository_records import MemoryRepositoryRecordsMixin  # noqa: F401
from .memory_repository_write import MemoryRepositoryWriteMixin  # noqa: F401
from .outbox import utc_now_iso


class SQLiteMemoryRepository(
    MemoryRepositoryReadMixin,
    MemoryRepositoryWriteMixin,
    MemoryRepositoryRecordsMixin,
):
    """SQLite WAL adapter for canonical memories and their side records."""

    def __init__(
        self,
        path: Path | str,
        *,
        busy_timeout_ms: int = MEMORY_STORE_BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = max(int(busy_timeout_ms), 100)
        self._initialize_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._initialized = False
        self._memory_columns_cache: frozenset[str] | None = None

    def _connect(self) -> sqlite3.Connection:
        assert_safe_path(self.path)
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

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            if self._schema_is_ready_without_writes():
                self._refresh_memory_columns()
                self._initialized = True
                return
            assert_safe_path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            assert_safe_path(self.path.parent, reject_hardlink_target=False)
            assert_safe_path(self.path)
            connection = self._connect()
            try:
                journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold()
                if journal_mode != "wal":
                    raise MemoryRepositoryError(
                        f"SQLite refused WAL mode for {self.path}: {journal_mode}"
                    )
                self._begin_immediate(connection)
                current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if current_version not in {0, *SUPPORTED_MEMORY_STORE_SCHEMA_VERSIONS}:
                    raise MemoryRepositoryError(
                        f"unsupported memory store schema {current_version}; "
                        f"expected {MEMORY_STORE_SCHEMA_VERSION}"
                    )
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS memory_store_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS memory_revisions (
                        revision_id TEXT PRIMARY KEY,
                        memory_id TEXT NOT NULL,
                        content TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        created_by TEXT,
                        metadata_json TEXT NOT NULL,
                        UNIQUE(memory_id, content_sha256)
                    );

                    CREATE TABLE IF NOT EXISTS memories (
                        memory_id TEXT PRIMARY KEY,
                        project TEXT NOT NULL,
                        memory_type TEXT NOT NULL,
                        lifecycle TEXT NOT NULL
                            CHECK (lifecycle IN ('active', 'archived', 'tombstoned')),
                        title TEXT,
                        summary TEXT,
                        tags_json TEXT NOT NULL,
                        files_json TEXT NOT NULL,
                        session_refs_json TEXT NOT NULL,
                        upsert_key TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        provenance_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        extra_json TEXT NOT NULL,
                        current_revision_id TEXT NOT NULL,
                        FOREIGN KEY (current_revision_id)
                            REFERENCES memory_revisions(revision_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_memories_project_lifecycle_time
                        ON memories(project, lifecycle, updated_at DESC, memory_id);
                    CREATE INDEX IF NOT EXISTS idx_memories_project_upsert
                        ON memories(project, upsert_key);
                    CREATE INDEX IF NOT EXISTS idx_memory_revisions_memory_time
                        ON memory_revisions(memory_id, created_at, revision_id);

                    CREATE TABLE IF NOT EXISTS memory_artifacts (
                        artifact_type TEXT NOT NULL,
                        artifact_id TEXT NOT NULL,
                        project TEXT NOT NULL,
                        memory_id TEXT,
                        lifecycle TEXT NOT NULL
                            CHECK (lifecycle IN ('active', 'archived', 'tombstoned')),
                        created_at TEXT,
                        updated_at TEXT,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (artifact_type, artifact_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_memory_artifacts_project_type_time
                        ON memory_artifacts(project, artifact_type, updated_at DESC, artifact_id);
                    CREATE INDEX IF NOT EXISTS idx_memory_artifacts_memory
                        ON memory_artifacts(memory_id);

                    CREATE TABLE IF NOT EXISTS memory_links (
                        link_id TEXT PRIMARY KEY,
                        project TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        relation TEXT NOT NULL,
                        created_at TEXT,
                        updated_at TEXT,
                        metadata_json TEXT NOT NULL,
                        UNIQUE(project, source_id, target_id, relation)
                    );

                    CREATE INDEX IF NOT EXISTS idx_memory_links_project_time
                        ON memory_links(project, updated_at DESC, link_id);
                    CREATE INDEX IF NOT EXISTS idx_memory_links_source
                        ON memory_links(source_id);
                    CREATE INDEX IF NOT EXISTS idx_memory_links_target
                        ON memory_links(target_id);

                    CREATE TABLE IF NOT EXISTS memory_outbox (
                        event_id TEXT PRIMARY KEY,
                        aggregate_type TEXT NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        event_version INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL
                            CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'dead_letter')),
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                        available_at TEXT NOT NULL,
                        claimed_at TEXT,
                        claim_token TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_memory_outbox_claimable
                        ON memory_outbox(status, available_at, created_at, event_id);
                    CREATE INDEX IF NOT EXISTS idx_memory_outbox_aggregate
                        ON memory_outbox(aggregate_type, aggregate_id, created_at);
                    """
                )
                connection.execute(
                    "INSERT OR REPLACE INTO memory_store_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(MEMORY_STORE_SCHEMA_VERSION)),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO memory_store_meta(key, value) VALUES (?, ?)",
                    ("created_at", utc_now_iso()),
                )
                connection.execute(f"PRAGMA user_version={MEMORY_STORE_SCHEMA_VERSION}")
                connection.execute("PRAGMA wal_autocheckpoint=1000")
                connection.commit()
                self._memory_columns_cache = frozenset(
                    str(row[1]) for row in connection.execute("PRAGMA table_info(memories)").fetchall()
                )
                self._initialized = True
            except Exception:  # broad: rollback transaction before re-raising to preserve repository failure semantics
                connection.rollback()
                raise
            finally:
                connection.close()

    def _schema_is_ready_without_writes(self, *, fast: bool = False) -> bool:
        """Return whether an existing target is usable without touching it."""

        assert_safe_path(self.path)
        if not self.path.exists():
            return False
        assert_safe_path(self.path)
        uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=self.busy_timeout_ms / 1000)
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in SUPPORTED_MEMORY_STORE_SCHEMA_VERSIONS:
                    return False
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                if not _REQUIRED_MEMORY_STORE_TABLES.issubset(tables):
                    return False
                if version >= FRESHNESS_SCHEMA_VERSION and not FRESHNESS_SCHEMA_TABLES.issubset(tables):
                    return False
                if version >= GOVERNED_CONSOLIDATION_SCHEMA_VERSION and not GOVERNED_CONSOLIDATION_SCHEMA_TABLES.issubset(tables):
                    return False
                if version >= PROJECTION_GENERATION_BINDING_SCHEMA_VERSION and not PROJECTION_GENERATION_BINDING_SCHEMA_TABLES.issubset(tables):
                    return False
                return fast or str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold() == "ok"
            finally:
                connection.close()
        except sqlite3.Error:
            return False

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            if not self._schema_is_ready_without_writes(fast=True):
                self.initialize()
            # A fast read check must not mark the repository fully initialized:
            # the first write still has to pass the integrity-complete
            # ``initialize()`` gate (including PRAGMA quick_check).

    @staticmethod
    def _begin_immediate(connection: sqlite3.Connection) -> None:
        for attempt, delay in enumerate((0.0, *MEMORY_STORE_WRITE_RETRY_DELAYS)):
            if delay:
                time.sleep(delay)
            try:
                connection.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).casefold() or attempt >= len(MEMORY_STORE_WRITE_RETRY_DELAYS):
                    raise

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        # Writes retain the integrity-complete initializer; read paths use the
        # shape-only fast initializer below and must not weaken this gate.
        self.initialize()
        with self._write_lock:
            connection = self._connect()
            try:
                self._begin_immediate(connection)
                yield connection
                connection.commit()
            except Exception:  # broad: rollback transaction before re-raising to preserve repository failure semantics
                connection.rollback()
                raise
            finally:
                connection.close()

    def _read_connection(self) -> sqlite3.Connection:
        self._ensure_initialized()
        return self._connect()

    def _refresh_memory_columns(self) -> frozenset[str]:
        if self._memory_columns_cache is not None:
            return self._memory_columns_cache
        connection = self._connect()
        try:
            self._memory_columns_cache = frozenset(
                str(row[1]) for row in connection.execute("PRAGMA table_info(memories)").fetchall()
            )
            return self._memory_columns_cache
        finally:
            connection.close()

    def health(self) -> RepositoryHealth:
        connection = self._read_connection()
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold()
            return RepositoryHealth(
                schema_version=version,
                journal_mode=journal_mode,
                quick_check=quick_check,
            )
        finally:
            connection.close()
