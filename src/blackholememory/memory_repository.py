"""Transactional SQLite repository for the Phase 2 memory aggregate.

The repository owns durable canonical rows and appends their domain events in
the same SQLite transaction. Mem0/Qdrant projections remain separate consumers
of the outbox and are not touched by this adapter.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .domain import Artifact
from .domain import Lifecycle
from .domain import Memory
from .domain import MemoryLink
from .domain import MemoryRevision
from .filesystem_boundaries import assert_safe_path
from .outbox import OutboxEvent
from .outbox import OutboxLeaseLost
from .outbox import OutboxStatus
from .outbox import utc_now_iso
from .temporal_contract import normalize_temporal_timestamp


MEMORY_STORE_SCHEMA_VERSION = 1
FRESHNESS_SCHEMA_VERSION = 2
MEMORY_STORE_SCHEMA_LATEST_VERSION = FRESHNESS_SCHEMA_VERSION
SUPPORTED_MEMORY_STORE_SCHEMA_VERSIONS = frozenset(
    {MEMORY_STORE_SCHEMA_VERSION, FRESHNESS_SCHEMA_VERSION}
)
FRESHNESS_SCHEMA_TABLES = frozenset(
    {"freshness_candidates", "freshness_candidate_events", "freshness_scan_state"}
)
MEMORY_STORE_BUSY_TIMEOUT_MS = 5_000
MEMORY_STORE_WRITE_RETRY_DELAYS = (0.025, 0.05, 0.1, 0.2, 0.4)
_REQUIRED_MEMORY_STORE_TABLES = frozenset(
    {
        "memory_store_meta",
        "memory_revisions",
        "memories",
        "memory_artifacts",
        "memory_links",
        "memory_outbox",
    }
)


class MemoryRepositoryError(RuntimeError):
    """Base error for repository and schema failures."""


class MemoryNotFoundError(MemoryRepositoryError):
    """Raised when an operation requires a memory that is not present."""


class MemoryRevisionConflict(MemoryRepositoryError):
    """Raised when optimistic concurrency sees a newer current revision."""


class MemoryRepositoryIntegrityError(MemoryRepositoryError):
    """Raised when persisted rows contradict the canonical aggregate."""


@dataclass(frozen=True)
class SaveMemoryResult:
    memory: Memory
    inserted: bool
    revision_inserted: bool
    outbox_event_id: str
    deduplicated: bool = False


@dataclass(frozen=True)
class RepositoryHealth:
    schema_version: int
    journal_mode: str
    quick_check: str


class MemoryRepository(Protocol):
    """Storage contract consumed by future services and projectors."""

    def initialize(self) -> None: ...

    def save_memory(
        self,
        memory: Memory,
        *,
        expected_revision_id: str | None = None,
    ) -> SaveMemoryResult: ...

    def save_memories_atomic(
        self,
        memories: Iterable[Memory],
        *,
        expected_revision_ids: Mapping[str, str | None] | None = None,
    ) -> list[SaveMemoryResult]: ...

    def save_memories_refinery_atomic(
        self,
        memories: Iterable[Memory],
        *,
        expected_memories: Mapping[str, Memory],
        project_aliases: Mapping[str, str],
    ) -> dict[str, Any]: ...

    def tombstone_project(self, project: str, *, reason: str = "project_retirement") -> dict[str, Any]: ...

    def get_memory(self, memory_id: str, *, project: str | None = None) -> Memory | None: ...

    def get_memories(self, memory_ids: Iterable[str], *, project: str | None = None) -> list[Memory]: ...

    def get_memory_by_upsert_key(
        self,
        project: str,
        upsert_key: str,
        *,
        include_archived: bool = False,
    ) -> Memory | None: ...

    def list_memories(
        self,
        *,
        project: str | None = None,
        memory_class: str | None = None,
        event_role: str | None = None,
        as_of: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        include_temporal_unknown: bool = False,
        include_archived: bool = False,
        include_tombstoned: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Memory]: ...

    def count_memories(
        self,
        *,
        project: str | None = None,
        memory_class: str | None = None,
        event_role: str | None = None,
        as_of: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        include_temporal_unknown: bool = False,
        include_archived: bool = False,
        include_tombstoned: bool = False,
    ) -> int: ...

    def list_projects(self, *, include_archived: bool = False) -> list[str]: ...

    def save_artifact(self, artifact: Artifact) -> Artifact: ...

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        project: str | None = None,
        include_archived: bool = False,
        include_tombstoned: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Artifact]: ...

    def save_link(self, link: MemoryLink) -> MemoryLink: ...

    def list_links(
        self,
        *,
        project: str | None = None,
        memory_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryLink]: ...

    def list_outbox(
        self,
        *,
        status: OutboxStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[OutboxEvent]: ...

    def claim_outbox(self, *, limit: int = 10, lease_seconds: float = 120.0) -> list[OutboxEvent]: ...

    def ack_outbox(self, event_id: str, claim_token: str) -> OutboxEvent: ...

    def defer_outbox(
        self,
        event_id: str,
        claim_token: str,
        error: str,
        *,
        retry_after_seconds: float = 5.0,
    ) -> OutboxEvent: ...

    def fail_outbox(
        self,
        event_id: str,
        claim_token: str,
        error: str,
        *,
        retry_after_seconds: float = 5.0,
        max_attempts: int = 5,
    ) -> OutboxEvent: ...


def _json_dumps(value: Any, field_name: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise MemoryRepositoryIntegrityError(f"{field_name} is not JSON serializable") from exc


def _json_loads(value: str, field_name: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MemoryRepositoryIntegrityError(f"persisted {field_name} is invalid JSON") from exc


def _bounded_page(limit: int, offset: int) -> tuple[int, int]:
    if limit < 1 or limit > 10_000:
        raise ValueError("limit must be between 1 and 10000")
    if offset < 0:
        raise ValueError("offset must not be negative")
    return limit, offset


def _memory_event_type(memory: Memory, *, inserted: bool) -> str:
    if inserted:
        return "memory.created"
    if memory.metadata.get("restored_at"):
        return "memory.restored"
    if memory.lifecycle is Lifecycle.ARCHIVED:
        return "memory.archived"
    if memory.lifecycle is Lifecycle.TOMBSTONED:
        return "memory.tombstoned"
    return "memory.updated"


def _memory_event_id(memory: Memory, event_type: str) -> str:
    fingerprint = ":".join(
        (
            "memory",
            memory.id,
            event_type,
            memory.current_revision.revision_id,
        )
    )
    return f"evt_bhm_{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:24]}"


class SQLiteMemoryRepository:
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
            except Exception:
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
            except Exception:
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

    @staticmethod
    def _revision_row_to_model(row: sqlite3.Row) -> MemoryRevision:
        return MemoryRevision(
            revision_id=str(row["revision_id"]),
            memory_id=str(row["memory_id"]),
            content=str(row["content"]),
            content_sha256=str(row["content_sha256"]),
            created_at=str(row["created_at"]),
            created_by=row["created_by"],
            metadata=_json_loads(str(row["metadata_json"]), "revision.metadata"),
        )

    @staticmethod
    def _outbox_row_to_model(row: sqlite3.Row) -> OutboxEvent:
        return OutboxEvent(
            event_id=str(row["event_id"]),
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=str(row["aggregate_id"]),
            event_type=str(row["event_type"]),
            event_version=int(row["event_version"]),
            payload=_json_loads(str(row["payload_json"]), "outbox.payload"),
            status=OutboxStatus(str(row["status"])),
            attempts=int(row["attempts"]),
            available_at=str(row["available_at"]),
            claimed_at=row["claimed_at"],
            claim_token=row["claim_token"],
            last_error=row["last_error"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _append_memory_event(
        self,
        connection: sqlite3.Connection,
        memory: Memory,
        *,
        inserted: bool,
    ) -> str:
        now = utc_now_iso()
        payload_json = _json_dumps(memory.to_dict(), "outbox.payload")
        same_payload = connection.execute(
            "SELECT event_id FROM memory_outbox "
            "WHERE aggregate_type = 'memory' AND aggregate_id = ? AND payload_json = ? "
            "ORDER BY created_at DESC, event_id LIMIT 1",
            (memory.id, payload_json),
        ).fetchone()
        if same_payload is not None:
            return str(same_payload["event_id"])
        event_type = _memory_event_type(memory, inserted=inserted)
        event_id = _memory_event_id(memory, event_type)
        existing = connection.execute(
            "SELECT aggregate_type, aggregate_id, event_type, payload_json FROM memory_outbox "
            "WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["aggregate_type"]) != "memory"
                or str(existing["aggregate_id"]) != memory.id
                or str(existing["event_type"]) != event_type
                or str(existing["payload_json"]) != payload_json
            ):
                event_id = (
                    f"{event_id}_"
                    f"{hashlib.sha256(payload_json.encode('utf-8')).hexdigest()[:16]}"
                )
                existing = connection.execute(
                    "SELECT aggregate_type, aggregate_id, event_type, payload_json "
                    "FROM memory_outbox WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["aggregate_type"]) != "memory"
                        or str(existing["aggregate_id"]) != memory.id
                        or str(existing["event_type"]) != event_type
                        or str(existing["payload_json"]) != payload_json
                    ):
                        raise MemoryRepositoryIntegrityError(f"outbox event id collision: {event_id}")
                    return event_id
            else:
                return event_id
        event = OutboxEvent(
            event_id=event_id,
            aggregate_type="memory",
            aggregate_id=memory.id,
            event_type=event_type,
            event_version=1,
            payload=memory.to_dict(),
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        connection.execute(
            """
            INSERT INTO memory_outbox(
                event_id, aggregate_type, aggregate_id, event_type, event_version,
                payload_json, status, attempts, available_at, claimed_at,
                claim_token, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.aggregate_type,
                event.aggregate_id,
                event.event_type,
                event.event_version,
                payload_json,
                event.status.value,
                event.attempts,
                event.available_at,
                event.claimed_at,
                event.claim_token,
                event.last_error,
                event.created_at,
                event.updated_at,
            ),
        )
        return event_id

    @staticmethod
    def _joined_memory_query(where: str = "") -> str:
        return (
            "SELECT m.*, "
            "r.revision_id AS joined_revision_id, "
            "r.memory_id AS joined_revision_memory_id, "
            "r.content AS joined_revision_content, "
            "r.content_sha256 AS joined_revision_content_sha256, "
            "r.created_at AS joined_revision_created_at, "
            "r.created_by AS joined_revision_created_by, "
            "r.metadata_json AS joined_revision_metadata_json "
            "FROM memories AS m "
            "LEFT JOIN memory_revisions AS r ON r.revision_id = m.current_revision_id"
            f"{where}"
        )

    def _joined_memory_row_to_model(self, row: sqlite3.Row) -> Memory:
        revision_id = str(row["joined_revision_id"])
        revision_memory_id = str(row["joined_revision_memory_id"])
        if revision_id != str(row["current_revision_id"]) or revision_memory_id != str(row["memory_id"]):
            raise MemoryRepositoryIntegrityError(
                f"memory {row['memory_id']} references invalid revision {row['current_revision_id']}"
            )
        metadata = _json_loads(str(row["metadata_json"]), "memory.metadata")
        memory_class = (
            row["memory_class"]
            if "memory_class" in row.keys()
            else metadata.get("memory_class", "unclassified")
        )
        memory_class_source = (
            row["memory_class_source"]
            if "memory_class_source" in row.keys()
            else metadata.get("memory_class_source", "legacy-default")
        )
        memory_class_confidence = (
            row["memory_class_confidence"]
            if "memory_class_confidence" in row.keys()
            else metadata.get("memory_class_confidence")
        )
        event_role = (
            row["event_role"]
            if "event_role" in row.keys()
            else metadata.get("event_role", "unclassified")
        )
        event_role_version = (
            row["event_role_version"]
            if "event_role_version" in row.keys()
            else metadata.get("event_role_version", "1")
        )
        temporal_values = {
            "observed_at": row["observed_at"] if "observed_at" in row.keys() else metadata.get("observed_at"),
            "observed_at_source": (
                row["observed_at_source"] if "observed_at_source" in row.keys() else metadata.get("observed_at_source")
            ) or "legacy-unknown",
            "valid_from": row["valid_from"] if "valid_from" in row.keys() else metadata.get("valid_from"),
            "valid_to": row["valid_to"] if "valid_to" in row.keys() else metadata.get("valid_to"),
            "open_interval": (
                bool(row["open_interval"]) if "open_interval" in row.keys() else metadata.get("open_interval", True)
            ),
            "supersedes_revision_id": (
                row["supersedes_revision_id"]
                if "supersedes_revision_id" in row.keys()
                else metadata.get("supersedes_revision_id")
            ),
            "source_episode_id": (
                row["source_episode_id"] if "source_episode_id" in row.keys() else metadata.get("source_episode_id")
            ),
            "source_uri": row["source_uri"] if "source_uri" in row.keys() else metadata.get("source_uri"),
            "source_digest": row["source_digest"] if "source_digest" in row.keys() else metadata.get("source_digest"),
        }
        procedure_contract = metadata.get("procedure_contract")
        procedure_trace_receipt = metadata.get("procedure_trace_receipt")
        return Memory.from_dict(
            {
                "id": row["memory_id"],
                "project": row["project"],
                "memory_type": row["memory_type"],
                "memory_class": memory_class,
                "memory_class_source": memory_class_source,
                "memory_class_confidence": memory_class_confidence,
                "event_role": event_role,
                "event_role_version": event_role_version,
                **temporal_values,
                "procedure_contract": procedure_contract,
                "procedure_trace_receipt": procedure_trace_receipt,
                "lifecycle": row["lifecycle"],
                "title": row["title"],
                "summary": row["summary"],
                "tags": _json_loads(str(row["tags_json"]), "memory.tags"),
                "files": _json_loads(str(row["files_json"]), "memory.files"),
                "session_refs": _json_loads(str(row["session_refs_json"]), "memory.session_refs"),
                "upsert_key": row["upsert_key"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "current_revision": {
                    "revision_id": revision_id,
                    "memory_id": revision_memory_id,
                    "content": str(row["joined_revision_content"]),
                    "content_sha256": str(row["joined_revision_content_sha256"]),
                    "created_at": str(row["joined_revision_created_at"]),
                    "created_by": row["joined_revision_created_by"],
                    "metadata": _json_loads(
                        str(row["joined_revision_metadata_json"]),
                        "revision.metadata",
                    ),
                },
                "provenance": _json_loads(str(row["provenance_json"]), "memory.provenance"),
                "metadata": metadata,
                "extra": _json_loads(str(row["extra_json"]), "memory.extra"),
            }
        )

    def _save_memory_in_transaction(
        self,
        connection: sqlite3.Connection,
        memory: Memory,
        *,
        expected_revision_id: str | None = None,
    ) -> SaveMemoryResult:
        existing = connection.execute(
            "SELECT current_revision_id FROM memories WHERE memory_id = ?",
            (memory.id,),
        ).fetchone()
        inserted = existing is None
        current_revision_id = str(existing["current_revision_id"]) if existing else None
        if (
            not inserted
            and expected_revision_id is not None
            and current_revision_id != expected_revision_id
        ):
            raise MemoryRevisionConflict(
                f"memory {memory.id} changed: expected revision {expected_revision_id}, "
                f"found {current_revision_id}"
            )

        deduplicated = False
        stored_revision = connection.execute(
            "SELECT * FROM memory_revisions WHERE revision_id = ?",
            (memory.current_revision.revision_id,),
        ).fetchone()
        if stored_revision is not None:
            if (
                str(stored_revision["memory_id"]) != memory.id
                or str(stored_revision["content_sha256"]) != memory.current_revision.content_sha256
                or str(stored_revision["content"]) != memory.current_revision.content
            ):
                raise MemoryRepositoryIntegrityError(
                    f"revision id collision: {memory.current_revision.revision_id}"
                )
            memory = memory.model_copy(
                update={"current_revision": self._revision_row_to_model(stored_revision)}
            )
            revision_inserted = False
        else:
            duplicate_hash = connection.execute(
                "SELECT * FROM memory_revisions "
                "WHERE memory_id = ? AND content_sha256 = ?",
                (memory.id, memory.current_revision.content_sha256),
            ).fetchone()
            if duplicate_hash is not None:
                if str(duplicate_hash["content"]) != memory.current_revision.content:
                    raise MemoryRepositoryIntegrityError(
                        "content hash collision for memory "
                        f"{memory.id}: {memory.current_revision.content_sha256}"
                    )
                memory = memory.model_copy(
                    update={"current_revision": self._revision_row_to_model(duplicate_hash)}
                )
                revision_inserted = False
                deduplicated = True
            else:
                connection.execute(
                    """
                    INSERT INTO memory_revisions(
                        revision_id, memory_id, content, content_sha256,
                        created_at, created_by, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory.current_revision.revision_id,
                        memory.id,
                        memory.current_revision.content,
                        memory.current_revision.content_sha256,
                        memory.current_revision.created_at,
                        memory.current_revision.created_by,
                        _json_dumps(memory.current_revision.metadata, "revision.metadata"),
                    ),
                )
                revision_inserted = True

        memory_columns = (
            self._memory_columns_cache
            if self._memory_columns_cache is not None
            else self._refresh_memory_columns()
        )
        has_memory_class_columns = {
            "memory_class",
            "memory_class_source",
            "memory_class_confidence",
        }.issubset(memory_columns)
        has_event_role_columns = {"event_role", "event_role_version"}.issubset(memory_columns)
        has_temporal_columns = {
            "observed_at",
            "observed_at_source",
            "valid_from",
            "valid_to",
            "open_interval",
            "supersedes_revision_id",
            "source_episode_id",
            "source_uri",
            "source_digest",
        }.issubset(memory_columns)
        column_values: list[tuple[str, Any]] = [
            ("memory_id", memory.id),
            ("project", memory.project),
            ("memory_type", memory.memory_type),
        ]
        if has_memory_class_columns:
            column_values.extend(
                [
                    ("memory_class", memory.memory_class.value),
                    ("memory_class_source", memory.memory_class_source.value),
                    ("memory_class_confidence", memory.memory_class_confidence),
                ]
            )
        if has_event_role_columns:
            column_values.extend(
                [
                    ("event_role", memory.event_role.value),
                    ("event_role_version", memory.event_role_version),
                ]
            )
        if has_temporal_columns:
            column_values.extend(
                [
                    ("observed_at", memory.observed_at),
                    ("observed_at_source", memory.observed_at_source),
                    ("valid_from", memory.valid_from),
                    ("valid_to", memory.valid_to),
                    ("open_interval", int(memory.open_interval)),
                    ("supersedes_revision_id", memory.supersedes_revision_id),
                    ("source_episode_id", memory.source_episode_id),
                    ("source_uri", memory.source_uri),
                    ("source_digest", memory.source_digest),
                ]
            )
        column_values.extend(
            [
                ("lifecycle", memory.lifecycle.value),
                ("title", memory.title),
                ("summary", memory.summary),
                ("tags_json", _json_dumps(list(memory.tags), "memory.tags")),
                ("files_json", _json_dumps(list(memory.files), "memory.files")),
                ("session_refs_json", _json_dumps(list(memory.session_refs), "memory.session_refs")),
                ("upsert_key", memory.upsert_key),
                ("created_at", memory.created_at),
                ("updated_at", memory.updated_at),
                ("provenance_json", _json_dumps(memory.provenance.to_dict(), "memory.provenance")),
                ("metadata_json", _json_dumps(memory.metadata, "memory.metadata")),
                ("extra_json", _json_dumps(memory.extra, "memory.extra")),
                ("current_revision_id", memory.current_revision.revision_id),
            ]
        )
        if inserted:
            names = ", ".join(name for name, _value in column_values)
            placeholders = ", ".join("?" for _name, _value in column_values)
            connection.execute(
                f"INSERT INTO memories({names}) VALUES ({placeholders})",
                tuple(value for _name, value in column_values),
            )
        else:
            assignments = ", ".join(
                f"{name} = ?" for name, _value in column_values if name != "memory_id"
            )
            connection.execute(
                f"UPDATE memories SET {assignments} WHERE memory_id = ?",
                tuple(value for name, value in column_values if name != "memory_id") + (memory.id,),
            )
        outbox_event_id = self._append_memory_event(
            connection,
            memory,
            inserted=inserted,
        )
        return SaveMemoryResult(
            memory=memory,
            inserted=inserted,
            revision_inserted=revision_inserted,
            outbox_event_id=outbox_event_id,
            deduplicated=deduplicated,
        )

    def save_memory(
        self,
        memory: Memory,
        *,
        expected_revision_id: str | None = None,
    ) -> SaveMemoryResult:
        """Atomically persist an aggregate and its current immutable revision."""

        with self._write_transaction() as connection:
            return self._save_memory_in_transaction(
                connection,
                memory,
                expected_revision_id=expected_revision_id,
            )

    def save_memories_atomic(
        self,
        memories: Iterable[Memory],
        *,
        expected_revision_ids: Mapping[str, str | None] | None = None,
    ) -> list[SaveMemoryResult]:
        """Persist a bounded memory batch and all outbox events in one transaction."""

        items = list(memories)
        ids = [memory.id for memory in items]
        if len(ids) != len(set(ids)):
            raise MemoryRepositoryIntegrityError("atomic memory batch contains duplicate memory ids")
        if len(items) > 10_000:
            raise ValueError("atomic memory batch exceeds 10000 items")
        expected = dict(expected_revision_ids or {})
        with self._write_transaction() as connection:
            return [
                self._save_memory_in_transaction(
                    connection,
                    memory,
                    expected_revision_id=expected.get(memory.id),
                )
                for memory in items
            ]

    def save_memories_refinery_atomic(
        self,
        memories: Iterable[Memory],
        *,
        expected_memories: Mapping[str, Memory],
        project_aliases: Mapping[str, str],
    ) -> dict[str, Any]:
        """Apply a refinery batch with full-record CAS and related project updates."""

        items = list(memories)
        ids = [memory.id for memory in items]
        if len(ids) != len(set(ids)):
            raise MemoryRepositoryIntegrityError("refinery batch contains duplicate memory ids")
        if not set(ids).issubset(expected_memories):
            raise MemoryRepositoryIntegrityError("refinery batch is outside the expected snapshot")
        if len(items) > 10_000:
            raise ValueError("refinery batch exceeds 10000 items")
        aliases = {
            str(source): str(target)
            for source, target in project_aliases.items()
            if str(source) and str(target) and str(source) != str(target)
        }
        with self._write_transaction() as connection:
            rows = connection.execute(self._joined_memory_query()).fetchall()
            actual_memories = {
                memory.id: memory
                for memory in (self._joined_memory_row_to_model(row) for row in rows)
            }
            if set(actual_memories) != set(expected_memories):
                raise MemoryRevisionConflict("authoritative memory set changed before refinery apply")
            for memory_id, expected in expected_memories.items():
                actual = actual_memories[memory_id]
                if actual.to_record() != expected.to_record():
                    raise MemoryRevisionConflict(
                        f"memory {memory_id} changed before refinery apply"
                    )

            for source, target in aliases.items():
                upsert_collision = connection.execute(
                    """
                    SELECT legacy.memory_id
                    FROM memories AS legacy
                    JOIN memories AS canonical
                      ON canonical.project = ?
                     AND canonical.upsert_key = legacy.upsert_key
                     AND canonical.memory_id <> legacy.memory_id
                    WHERE legacy.project = ?
                      AND legacy.upsert_key IS NOT NULL
                    LIMIT 1
                    """,
                    (target, source),
                ).fetchone()
                if upsert_collision is not None:
                    raise MemoryRepositoryIntegrityError(
                        f"project alias {source!r}->{target!r} collides on upsert_key"
                    )
                collision = connection.execute(
                    """
                    SELECT legacy.link_id
                    FROM memory_links AS legacy
                    JOIN memory_links AS canonical
                      ON canonical.project = ?
                     AND canonical.source_id = legacy.source_id
                     AND canonical.target_id = legacy.target_id
                     AND canonical.relation = legacy.relation
                     AND canonical.link_id <> legacy.link_id
                    WHERE legacy.project = ?
                    LIMIT 1
                    """,
                    (target, source),
                ).fetchone()
                if collision is not None:
                    raise MemoryRepositoryIntegrityError(
                        f"project alias {source!r}->{target!r} collides in memory_links"
                    )

            results = [
                self._save_memory_in_transaction(
                    connection,
                    memory,
                    expected_revision_id=expected_memories[memory.id].current_revision.revision_id,
                )
                for memory in items
            ]
            links_updated = 0
            artifacts_updated = 0
            for source, target in aliases.items():
                links_updated += int(
                    connection.execute(
                        "UPDATE memory_links SET project = ? WHERE project = ?",
                        (target, source),
                    ).rowcount
                )
                artifacts_updated += int(
                    connection.execute(
                        "UPDATE memory_artifacts SET project = ? WHERE project = ?",
                        (target, source),
                    ).rowcount
                )
            return {
                "results": results,
                "links_updated": links_updated,
                "artifacts_updated": artifacts_updated,
                "project_aliases": aliases,
            }

    def get_memory(self, memory_id: str, *, project: str | None = None) -> Memory | None:
        connection = self._read_connection()
        try:
            if project is None:
                row = connection.execute(
                    self._joined_memory_query(" WHERE m.memory_id = ?"),
                    (memory_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    self._joined_memory_query(" WHERE m.memory_id = ? AND m.project = ?"),
                    (memory_id, project),
                ).fetchone()
            return self._joined_memory_row_to_model(row) if row is not None else None
        finally:
            connection.close()

    def get_memories(self, memory_ids: Iterable[str], *, project: str | None = None) -> list[Memory]:
        ids = tuple(dict.fromkeys(str(memory_id).strip() for memory_id in memory_ids if str(memory_id).strip()))
        if not ids:
            return []
        if len(ids) > 10_000:
            raise ValueError("memory id lookup exceeds 10000 items")
        connection = self._read_connection()
        try:
            found: dict[str, Memory] = {}
            for start in range(0, len(ids), 900):
                batch = ids[start : start + 900]
                placeholders = ",".join("?" for _ in batch)
                where = f" WHERE m.memory_id IN ({placeholders})"
                parameters: tuple[Any, ...] = batch
                if project is not None:
                    where += " AND m.project = ?"
                    parameters += (project,)
                rows = connection.execute(
                    self._joined_memory_query(where),
                    parameters,
                ).fetchall()
                for row in rows:
                    memory = self._joined_memory_row_to_model(row)
                    found[memory.id] = memory
            return [found[memory_id] for memory_id in ids if memory_id in found]
        finally:
            connection.close()

    def get_memory_by_upsert_key(
        self,
        project: str,
        upsert_key: str,
        *,
        include_archived: bool = False,
    ) -> Memory | None:
        project_id = str(project or "").strip()
        key = str(upsert_key or "").strip()
        if not project_id or not key:
            return None
        lifecycle_clause = "" if include_archived else " AND m.lifecycle = 'active'"
        connection = self._read_connection()
        try:
            row = connection.execute(
                self._joined_memory_query(
                    " WHERE m.project = ? AND m.upsert_key = ?"
                    + lifecycle_clause
                    + " ORDER BY m.updated_at DESC, m.memory_id LIMIT 1"
                ),
                (project_id, key),
            ).fetchone()
            return self._joined_memory_row_to_model(row) if row is not None else None
        finally:
            connection.close()

    def tombstone_project(self, project: str, *, reason: str = "project_retirement") -> dict[str, Any]:
        """Atomically tombstone every non-tombstoned memory in one project.

        Project retirement is a lifecycle operation, not a physical delete. The
        immutable content revision is reused (the schema deliberately
        de-duplicates identical content revisions); the lifecycle metadata and
        a normal memory outbox event are updated in the same SQLite transaction.
        """

        project_id = str(project or "").strip()
        if not project_id:
            raise ValueError("project is required")
        self.initialize()
        with self._write_transaction() as connection:
            return self.tombstone_project_in_transaction(connection, project_id, reason=reason)

    def tombstone_project_in_transaction(
        self,
        connection: sqlite3.Connection,
        project: str,
        *,
        reason: str = "project_retirement",
    ) -> dict[str, Any]:
        """Tombstone a project using a caller-owned already-open SQLite transaction.

        Composite lifecycle operations such as project retirement need the memory
        transition, dependent artifact updates and their receipt to commit or roll
        back together. The caller is responsible for opening and completing the
        transaction; this helper deliberately never commits or rolls back it.
        """

        project_id = str(project or "").strip()
        if not project_id:
            raise ValueError("project is required")
        now = utc_now_iso()
        updated: list[dict[str, Any]] = []
        rows = connection.execute(
            self._joined_memory_query(
                " WHERE m.project = ? AND m.lifecycle <> 'tombstoned' "
                "ORDER BY m.memory_id"
            ),
            (project_id,),
        ).fetchall()
        for row in rows:
            memory = self._joined_memory_row_to_model(row)
            payload = memory.to_dict()
            metadata = dict(memory.metadata)
            metadata["previous_lifecycle"] = memory.lifecycle.value
            metadata["tombstoned_at"] = now
            metadata["tombstone_reason"] = str(reason or "project_retirement")[:256]
            payload["lifecycle"] = Lifecycle.TOMBSTONED.value
            payload["metadata"] = metadata
            payload["updated_at"] = now
            tombstoned = Memory.from_dict(payload)
            connection.execute(
                "UPDATE memories SET lifecycle = ?, metadata_json = ?, updated_at = ? "
                "WHERE memory_id = ? AND project = ?",
                (
                    tombstoned.lifecycle.value,
                    _json_dumps(tombstoned.metadata, "memory.metadata"),
                    tombstoned.updated_at,
                    tombstoned.id,
                    project_id,
                ),
            )
            self._append_memory_event(connection, tombstoned, inserted=False)
            updated.append(tombstoned.to_record())
        return {"project": project_id, "count": len(updated), "memories": updated}

    def list_memories(
        self,
        *,
        project: str | None = None,
        memory_class: str | None = None,
        event_role: str | None = None,
        as_of: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        include_temporal_unknown: bool = False,
        include_archived: bool = False,
        include_tombstoned: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Memory]:
        limit, offset = _bounded_page(limit, offset)
        connection = self._read_connection()
        try:
            memory_columns = (
                self._memory_columns_cache
                if self._memory_columns_cache is not None
                else self._refresh_memory_columns()
            )
            clauses: list[str] = []
            parameters: list[Any] = []
            if project is not None:
                clauses.append("m.project = ?")
                parameters.append(project)
            if memory_class is not None:
                expression = (
                    "m.memory_class"
                    if "memory_class" in memory_columns
                    else "COALESCE(json_extract(m.metadata_json, '$.memory_class'), 'unclassified')"
                )
                clauses.append(f"{expression} = ?")
                parameters.append(memory_class)
            if event_role is not None:
                expression = (
                    "m.event_role"
                    if "event_role" in memory_columns
                    else "COALESCE(json_extract(m.metadata_json, '$.event_role'), 'unclassified')"
                )
                clauses.append(f"{expression} = ?")
                parameters.append(event_role)
            temporal_requested = any(value is not None for value in (as_of, valid_from, valid_to))
            if temporal_requested:
                observed_expression = "m.observed_at" if "observed_at" in memory_columns else "json_extract(m.metadata_json, '$.observed_at')"
                valid_from_expression = "m.valid_from" if "valid_from" in memory_columns else "json_extract(m.metadata_json, '$.valid_from')"
                valid_to_expression = "m.valid_to" if "valid_to" in memory_columns else "json_extract(m.metadata_json, '$.valid_to')"
                if as_of is not None:
                    normalized_as_of = normalize_temporal_timestamp(as_of, "as_of", allow_none=False)
                    if include_temporal_unknown:
                        clauses.append(f"({observed_expression} IS NULL OR {observed_expression} <= ?)")
                    else:
                        clauses.append(f"{observed_expression} IS NOT NULL AND {observed_expression} <= ?")
                    parameters.append(normalized_as_of)
                    clauses.append(f"({valid_from_expression} IS NULL OR {valid_from_expression} <= ?)")
                    parameters.append(normalized_as_of)
                    clauses.append(f"({valid_to_expression} IS NULL OR {valid_to_expression} > ?)")
                    parameters.append(normalized_as_of)
                if valid_from is not None or valid_to is not None:
                    normalized_from = normalize_temporal_timestamp(valid_from, "query_valid_from")
                    normalized_to = normalize_temporal_timestamp(valid_to, "query_valid_to")
                    if normalized_from is not None and normalized_to is not None and normalized_from >= normalized_to:
                        raise ValueError("query_valid_from must be earlier than query_valid_to")
                    if normalized_to is not None:
                        clauses.append(f"({valid_from_expression} IS NULL OR {valid_from_expression} < ?)")
                        parameters.append(normalized_to)
                    if normalized_from is not None:
                        clauses.append(f"({valid_to_expression} IS NULL OR {valid_to_expression} > ?)")
                        parameters.append(normalized_from)
            if not include_archived:
                clauses.append("m.lifecycle = 'active'")
            elif not include_tombstoned:
                clauses.append("m.lifecycle <> 'tombstoned'")
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                self._joined_memory_query(where)
                + " ORDER BY m.updated_at DESC, m.memory_id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
            return [self._joined_memory_row_to_model(row) for row in rows]
        finally:
            connection.close()

    def count_memories(
        self,
        *,
        project: str | None = None,
        memory_class: str | None = None,
        event_role: str | None = None,
        as_of: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        include_temporal_unknown: bool = False,
        include_archived: bool = False,
        include_tombstoned: bool = False,
    ) -> int:
        connection = self._read_connection()
        try:
            memory_columns = (
                self._memory_columns_cache
                if self._memory_columns_cache is not None
                else self._refresh_memory_columns()
            )
            clauses: list[str] = []
            parameters: list[Any] = []
            if project is not None:
                clauses.append("project = ?")
                parameters.append(project)
            if memory_class is not None:
                expression = (
                    "memory_class"
                    if "memory_class" in memory_columns
                    else "COALESCE(json_extract(metadata_json, '$.memory_class'), 'unclassified')"
                )
                clauses.append(f"{expression} = ?")
                parameters.append(memory_class)
            if event_role is not None:
                expression = (
                    "event_role"
                    if "event_role" in memory_columns
                    else "COALESCE(json_extract(metadata_json, '$.event_role'), 'unclassified')"
                )
                clauses.append(f"{expression} = ?")
                parameters.append(event_role)
            temporal_requested = any(value is not None for value in (as_of, valid_from, valid_to))
            if temporal_requested:
                observed_expression = "observed_at" if "observed_at" in memory_columns else "json_extract(metadata_json, '$.observed_at')"
                valid_from_expression = "valid_from" if "valid_from" in memory_columns else "json_extract(metadata_json, '$.valid_from')"
                valid_to_expression = "valid_to" if "valid_to" in memory_columns else "json_extract(metadata_json, '$.valid_to')"
                if as_of is not None:
                    normalized_as_of = normalize_temporal_timestamp(as_of, "as_of", allow_none=False)
                    if include_temporal_unknown:
                        clauses.append(f"({observed_expression} IS NULL OR {observed_expression} <= ?)")
                    else:
                        clauses.append(f"{observed_expression} IS NOT NULL AND {observed_expression} <= ?")
                    parameters.append(normalized_as_of)
                    clauses.append(f"({valid_from_expression} IS NULL OR {valid_from_expression} <= ?)")
                    parameters.append(normalized_as_of)
                    clauses.append(f"({valid_to_expression} IS NULL OR {valid_to_expression} > ?)")
                    parameters.append(normalized_as_of)
                if valid_from is not None or valid_to is not None:
                    normalized_from = normalize_temporal_timestamp(valid_from, "query_valid_from")
                    normalized_to = normalize_temporal_timestamp(valid_to, "query_valid_to")
                    if normalized_from is not None and normalized_to is not None and normalized_from >= normalized_to:
                        raise ValueError("query_valid_from must be earlier than query_valid_to")
                    if normalized_to is not None:
                        clauses.append(f"({valid_from_expression} IS NULL OR {valid_from_expression} < ?)")
                        parameters.append(normalized_to)
                    if normalized_from is not None:
                        clauses.append(f"({valid_to_expression} IS NULL OR {valid_to_expression} > ?)")
                        parameters.append(normalized_from)
            if not include_archived:
                clauses.append("lifecycle = 'active'")
            elif not include_tombstoned:
                clauses.append("lifecycle <> 'tombstoned'")
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            row = connection.execute(f"SELECT COUNT(*) FROM memories{where}", parameters).fetchone()
            return int(row[0] if row else 0)
        finally:
            connection.close()

    def list_projects(self, *, include_archived: bool = False) -> list[str]:
        clause = "" if include_archived else " WHERE lifecycle = 'active'"
        connection = self._read_connection()
        try:
            rows = connection.execute(
                "SELECT project, MAX(updated_at) AS latest FROM memories"
                + clause
                + " GROUP BY project ORDER BY latest DESC, project"
            ).fetchall()
            return [str(row["project"]) for row in rows if str(row["project"] or "").strip()]
        finally:
            connection.close()

    def save_artifact(self, artifact: Artifact) -> Artifact:
        with self._write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO memory_artifacts(
                    artifact_type, artifact_id, project, memory_id, lifecycle,
                    created_at, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_type, artifact_id) DO UPDATE SET
                    project = excluded.project,
                    memory_id = excluded.memory_id,
                    lifecycle = excluded.lifecycle,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
                """,
                (
                    artifact.artifact_type,
                    artifact.id,
                    artifact.project,
                    artifact.memory_id,
                    artifact.lifecycle.value,
                    artifact.created_at,
                    artifact.updated_at,
                    _json_dumps(artifact.payload, "artifact.payload"),
                ),
            )
        return artifact

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        project: str | None = None,
        include_archived: bool = False,
        include_tombstoned: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Artifact]:
        limit, offset = _bounded_page(limit, offset)
        clauses: list[str] = []
        parameters: list[Any] = []
        if artifact_type is not None:
            clauses.append("artifact_type = ?")
            parameters.append(artifact_type)
        if project is not None:
            clauses.append("project = ?")
            parameters.append(project)
        if not include_archived:
            clauses.append("lifecycle = 'active'")
        elif not include_tombstoned:
            clauses.append("lifecycle <> 'tombstoned'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self._read_connection()
        try:
            rows = connection.execute(
                "SELECT * FROM memory_artifacts" + where
                + " ORDER BY COALESCE(updated_at, created_at, '') DESC, artifact_id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
            return [
                Artifact(
                    id=str(row["artifact_id"]),
                    artifact_type=str(row["artifact_type"]),
                    project=str(row["project"]),
                    memory_id=row["memory_id"],
                    lifecycle=Lifecycle(str(row["lifecycle"])),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    payload=_json_loads(str(row["payload_json"]), "artifact.payload"),
                )
                for row in rows
            ]
        finally:
            connection.close()

    def save_link(self, link: MemoryLink) -> MemoryLink:
        with self._write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO memory_links(
                    link_id, project, source_id, target_id, relation,
                    created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(link_id) DO UPDATE SET
                    project = excluded.project,
                    source_id = excluded.source_id,
                    target_id = excluded.target_id,
                    relation = excluded.relation,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    link.id,
                    link.project,
                    link.source_id,
                    link.target_id,
                    link.relation,
                    link.created_at,
                    link.updated_at,
                    _json_dumps(link.metadata, "link.metadata"),
                ),
            )
        return link

    def list_links(
        self,
        *,
        project: str | None = None,
        memory_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryLink]:
        limit, offset = _bounded_page(limit, offset)
        clauses: list[str] = []
        parameters: list[Any] = []
        if project is not None:
            clauses.append("project = ?")
            parameters.append(project)
        if memory_id is not None:
            clauses.append("(source_id = ? OR target_id = ?)")
            parameters.extend([memory_id, memory_id])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        connection = self._read_connection()
        try:
            rows = connection.execute(
                "SELECT * FROM memory_links" + where
                + " ORDER BY COALESCE(updated_at, created_at, '') DESC, link_id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
            return [
                MemoryLink(
                    id=str(row["link_id"]),
                    project=str(row["project"]),
                    source_id=str(row["source_id"]),
                    target_id=str(row["target_id"]),
                    relation=str(row["relation"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    metadata=_json_loads(str(row["metadata_json"]), "link.metadata"),
                )
                for row in rows
            ]
        finally:
            connection.close()

    def get_outbox_event(self, event_id: str) -> OutboxEvent | None:
        connection = self._read_connection()
        try:
            row = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return self._outbox_row_to_model(row) if row is not None else None
        finally:
            connection.close()

    def list_outbox(
        self,
        *,
        status: OutboxStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[OutboxEvent]:
        limit, offset = _bounded_page(limit, offset)
        clause = " WHERE status = ?" if status is not None else ""
        parameters: tuple[Any, ...] = (status.value,) if status is not None else ()
        connection = self._read_connection()
        try:
            rows = connection.execute(
                "SELECT * FROM memory_outbox" + clause
                + " ORDER BY created_at, event_id LIMIT ? OFFSET ?",
                parameters + (limit, offset),
            ).fetchall()
            return [self._outbox_row_to_model(row) for row in rows]
        finally:
            connection.close()

    def claim_outbox(self, *, limit: int = 10, lease_seconds: float = 120.0) -> list[OutboxEvent]:
        if limit < 1 or limit > 1_000:
            raise ValueError("outbox claim limit must be between 1 and 1000")
        if lease_seconds <= 0 or lease_seconds > 86_400:
            raise ValueError("lease_seconds must be between 0 and 86400")
        now = utc_now_iso()
        expired_before = (
            datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)
        ).isoformat().replace("+00:00", "Z")
        with self._write_transaction() as connection:
            connection.execute(
                """
                UPDATE memory_outbox SET
                    status = 'pending', claimed_at = NULL, claim_token = NULL,
                    updated_at = ?
                WHERE status = 'processing' AND claimed_at IS NOT NULL AND claimed_at < ?
                """,
                (now, expired_before),
            )
            rows = connection.execute(
                """
                SELECT event_id FROM memory_outbox
                WHERE status IN ('pending', 'failed') AND available_at <= ?
                ORDER BY created_at, event_id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            claimed: list[OutboxEvent] = []
            for row in rows:
                event_id = str(row["event_id"])
                token = f"lease_bhm_{uuid4().hex}"
                connection.execute(
                    """
                    UPDATE memory_outbox SET
                        status = 'processing', attempts = attempts + 1,
                        claimed_at = ?, claim_token = ?, updated_at = ?
                    WHERE event_id = ? AND status IN ('pending', 'failed')
                    """,
                    (now, token, now, event_id),
                )
                claimed_row = connection.execute(
                    "SELECT * FROM memory_outbox WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if claimed_row is not None:
                    claimed.append(self._outbox_row_to_model(claimed_row))
            return claimed

    def ack_outbox(self, event_id: str, claim_token: str) -> OutboxEvent:
        now = utc_now_iso()
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None or row["status"] != OutboxStatus.PROCESSING.value or row["claim_token"] != claim_token:
                raise OutboxLeaseLost(f"outbox lease is not owned: {event_id}")
            connection.execute(
                """
                UPDATE memory_outbox SET
                    status = 'completed', claimed_at = NULL, claim_token = NULL,
                    updated_at = ?
                WHERE event_id = ?
                """,
                (now, event_id),
            )
            updated = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            assert updated is not None
            return self._outbox_row_to_model(updated)

    def defer_outbox(
        self,
        event_id: str,
        claim_token: str,
        error: str,
        *,
        retry_after_seconds: float = 5.0,
    ) -> OutboxEvent:
        """Release an owned lease without charging an infrastructure attempt.

        Claims increment ``attempts`` atomically.  A projection dependency
        outage is outside the event's control, so return the row to the
        pending queue and undo exactly that claim increment.  Domain/data
        failures continue through ``fail_outbox`` and retain the existing
        bounded retry/dead-letter contract.
        """

        if retry_after_seconds < 0 or retry_after_seconds > 86_400:
            raise ValueError("retry_after_seconds must be between 0 and 86400")
        now = utc_now_iso()
        available_at = (
            datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
        ).isoformat().replace("+00:00", "Z")
        bounded_error = str(error or "projection infrastructure unavailable")[:2_000]
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None or row["status"] != OutboxStatus.PROCESSING.value or row["claim_token"] != claim_token:
                raise OutboxLeaseLost(f"outbox lease is not owned: {event_id}")
            connection.execute(
                """
                UPDATE memory_outbox SET
                    status = 'pending', attempts = MAX(attempts - 1, 0),
                    available_at = ?, claimed_at = NULL, claim_token = NULL,
                    last_error = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (available_at, bounded_error, now, event_id),
            )
            updated = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            assert updated is not None
            return self._outbox_row_to_model(updated)

    def fail_outbox(
        self,
        event_id: str,
        claim_token: str,
        error: str,
        *,
        retry_after_seconds: float = 5.0,
        max_attempts: int = 5,
    ) -> OutboxEvent:
        if retry_after_seconds < 0 or retry_after_seconds > 86_400:
            raise ValueError("retry_after_seconds must be between 0 and 86400")
        if max_attempts < 1 or max_attempts > 100:
            raise ValueError("max_attempts must be between 1 and 100")
        now = utc_now_iso()
        bounded_error = str(error or "outbox processing failed")[:2_000]
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None or row["status"] != OutboxStatus.PROCESSING.value or row["claim_token"] != claim_token:
                raise OutboxLeaseLost(f"outbox lease is not owned: {event_id}")
            attempts = int(row["attempts"])
            terminal = attempts >= max_attempts
            available_at = now
            if not terminal:
                available_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
                ).isoformat().replace("+00:00", "Z")
            status = OutboxStatus.DEAD_LETTER.value if terminal else OutboxStatus.FAILED.value
            connection.execute(
                """
                UPDATE memory_outbox SET
                    status = ?, available_at = ?, claimed_at = NULL,
                    claim_token = NULL, last_error = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (status, available_at, bounded_error, now, event_id),
            )
            updated = connection.execute(
                "SELECT * FROM memory_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            assert updated is not None
            return self._outbox_row_to_model(updated)

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
