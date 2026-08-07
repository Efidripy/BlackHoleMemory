"""Disposable-first SQLite checkpoint saver for LangGraph.

This module deliberately does not wire a saver into the live developer graph.
The default constructor is disabled and refuses the authoritative BHM memory
database unless an explicit future operator gate is supplied.  The adapter
stores orchestration state only; it never calls BHM memory, Mem0, or Qdrant.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.base import ChannelVersions
from langgraph.checkpoint.base import Checkpoint
from langgraph.checkpoint.base import CheckpointMetadata
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.base import WRITES_IDX_MAP
from langgraph.checkpoint.base import get_checkpoint_metadata


CHECKPOINT_SCHEMA_VERSION = "bhm.langgraph.checkpoint.sqlite.v1"
DEFAULT_MAX_STATE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_WRITE_BYTES = 512 * 1024
DEFAULT_BUSY_TIMEOUT_MS = 5_000
_LOCK_RETRIES = 4
_SCOPE_LIMIT = 256
_IDENTIFIER_RE = re.compile(r"^[^\x00]{1,256}$")
_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_MARKERS = (
    "authorization",
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _text(value: Any, *, name: str, required: bool = True) -> str:
    result = str(value or "").strip()
    if not result:
        if required:
            raise ValueError(f"{name}_required")
        return ""
    if len(result) > _SCOPE_LIMIT or not _IDENTIFIER_RE.fullmatch(result):
        raise ValueError(f"{name}_invalid")
    return result


def _version_key(value: Any) -> str:
    result = str(value)
    if len(result) > _SCOPE_LIMIT or "\x00" in result:
        raise ValueError("checkpoint_version_invalid")
    return result


def _redact(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS):
        return _REDACTED
    if isinstance(value, Mapping):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, key=key) for item in value]
    if isinstance(value, set):
        return sorted(_redact(item, key=key) for item in value)
    return value


class SQLiteLangGraphCheckpointSaver(BaseCheckpointSaver[str]):
    """Versioned SQLite ``BaseCheckpointSaver`` with disposable-first guards.

    ``enabled`` defaults to ``False`` so merely constructing an object cannot
    create a schema or claim durable execution.  Tests and disposable drills
    pass ``enabled=True`` with a temporary path.  A future live activation must
    additionally pass ``allow_authoritative=True`` and is intentionally not
    performed by this remediation slice.
    """

    schema_version = CHECKPOINT_SCHEMA_VERSION

    def __init__(
        self,
        database_path: str | Path,
        *,
        project: str,
        caller_id: str,
        task_id: str,
        session_id: str,
        enabled: bool = False,
        allow_authoritative: bool = False,
        max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
        max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        serde: Any | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self.database_path = Path(database_path).expanduser().resolve()
        self.project = _text(project, name="project")
        self.caller_id = _text(caller_id, name="caller_id")
        self.task_id = _text(task_id, name="task_id")
        self.session_id = _text(session_id, name="session_id")
        self.enabled = bool(enabled)
        self.allow_authoritative = bool(allow_authoritative)
        self.max_state_bytes = self._positive_bound(max_state_bytes, "max_state_bytes")
        self.max_write_bytes = self._positive_bound(max_write_bytes, "max_write_bytes")
        self.busy_timeout_ms = self._positive_bound(busy_timeout_ms, "busy_timeout_ms")
        self._lock = threading.RLock()
        self._initialized = False
        if self.enabled:
            self._assert_disposable_path()
            self._ensure_schema()

    @staticmethod
    def _positive_bound(value: Any, name: str) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}_invalid") from exc
        if result <= 0:
            raise ValueError(f"{name}_invalid")
        return result

    @property
    def feature_state(self) -> str:
        return "enabled" if self.enabled else "disabled"

    def _assert_disposable_path(self) -> None:
        parts = {part.lower() for part in self.database_path.parts}
        is_authoritative_name = self.database_path.name.lower() == "memories.sqlite3"
        is_live_memory = "live-memory" in parts and is_authoritative_name
        if is_live_memory and not self.allow_authoritative:
            raise ValueError("authoritative_sqlite_requires_explicit_operator_gate")

    def _ensure_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError("langgraph_checkpoint_feature_disabled")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _ensure_schema(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS bhm_langgraph_checkpoint_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS bhm_langgraph_checkpoints (
                        project TEXT NOT NULL,
                        caller_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        checkpoint_ns TEXT NOT NULL,
                        checkpoint_id TEXT NOT NULL,
                        parent_checkpoint_id TEXT,
                        checkpoint_type TEXT NOT NULL,
                        checkpoint_blob BLOB NOT NULL,
                        checkpoint_digest TEXT NOT NULL,
                        metadata_type TEXT NOT NULL,
                        metadata_blob BLOB NOT NULL,
                        metadata_digest TEXT NOT NULL,
                        run_id TEXT,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (project, caller_id, thread_id, checkpoint_ns, checkpoint_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_bhm_lg_cp_thread
                        ON bhm_langgraph_checkpoints(project, caller_id, thread_id, checkpoint_ns, checkpoint_id);
                    CREATE INDEX IF NOT EXISTS idx_bhm_lg_cp_run
                        ON bhm_langgraph_checkpoints(project, caller_id, run_id);
                    CREATE TABLE IF NOT EXISTS bhm_langgraph_checkpoint_blobs (
                        project TEXT NOT NULL,
                        caller_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        checkpoint_ns TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        version TEXT NOT NULL,
                        value_type TEXT NOT NULL,
                        value_blob BLOB NOT NULL,
                        value_digest TEXT NOT NULL,
                        PRIMARY KEY (project, caller_id, thread_id, checkpoint_ns, channel, version)
                    );
                    CREATE TABLE IF NOT EXISTS bhm_langgraph_checkpoint_writes (
                        project TEXT NOT NULL,
                        caller_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        checkpoint_ns TEXT NOT NULL,
                        checkpoint_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        task_path TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        write_index INTEGER NOT NULL,
                        value_type TEXT NOT NULL,
                        value_blob BLOB NOT NULL,
                        value_digest TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (
                            project, caller_id, thread_id, checkpoint_ns,
                            checkpoint_id, task_id, task_path, channel, write_index
                        )
                    );
                    INSERT INTO bhm_langgraph_checkpoint_meta(key, value)
                        VALUES ('schema_version', 'bhm.langgraph.checkpoint.sqlite.v1')
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value;
                    """
                )
            finally:
                connection.close()
            self._initialized = True

    def _scope(self) -> tuple[str, str]:
        return self.project, self.caller_id

    def _validate_config(
        self,
        config: Mapping[str, Any],
        *,
        require_checkpoint_id: bool = False,
    ) -> tuple[dict[str, Any], str, str, str | None]:
        self._ensure_enabled()
        configurable = config.get("configurable") if isinstance(config, Mapping) else None
        if not isinstance(configurable, Mapping):
            raise ValueError("langgraph_configurable_scope_required")
        thread_id = _text(configurable.get("thread_id"), name="thread_id")
        checkpoint_ns = _text(
            configurable.get("checkpoint_ns", ""), name="checkpoint_ns", required=False
        )
        checkpoint_id_value = configurable.get("checkpoint_id")
        checkpoint_id = (
            _text(checkpoint_id_value, name="checkpoint_id")
            if checkpoint_id_value is not None
            else None
        )
        if require_checkpoint_id and checkpoint_id is None:
            raise ValueError("checkpoint_id_required")
        for key, expected in (
            ("project", self.project),
            ("caller_id", self.caller_id),
            ("task_id", self.task_id),
            ("session_id", self.session_id),
        ):
            actual = configurable.get(key)
            if actual is not None and str(actual) != expected:
                raise PermissionError(f"{key}_scope_mismatch")
        normalized = dict(config)
        normalized["configurable"] = dict(configurable)
        return normalized, thread_id, checkpoint_ns, checkpoint_id

    def _config(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None,
    ) -> dict[str, Any]:
        configurable: dict[str, Any] = {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "project": self.project,
            "caller_id": self.caller_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
        }
        if checkpoint_id is not None:
            configurable["checkpoint_id"] = checkpoint_id
        return {"configurable": configurable}

    def _dump(self, value: Any) -> tuple[str, bytes, str]:
        value_type, payload = self.serde.dumps_typed(value)
        raw = bytes(payload)
        return str(value_type), raw, _digest(raw)

    def _load(self, value_type: str, payload: bytes) -> Any:
        return self.serde.loads_typed((value_type, bytes(payload)))

    def _check_size(self, size: int, *, limit: int, error: str) -> None:
        if size > limit:
            raise ValueError(error)

    def _write_transaction(self, operation: Any) -> Any:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(_LOCK_RETRIES + 1):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = operation(connection)
                connection.execute("COMMIT")
                return result
            except sqlite3.OperationalError as exc:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                last_error = exc
                if attempt < _LOCK_RETRIES:
                    time.sleep(0.025 * (2**attempt))
            finally:
                connection.close()
        raise RuntimeError("sqlite_checkpoint_busy_retry_exhausted") from last_error

    def _checkpoint_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> CheckpointTuple:
        checkpoint = self._load(str(row["checkpoint_type"]), row["checkpoint_blob"])
        metadata = self._load(str(row["metadata_type"]), row["metadata_blob"])
        if not isinstance(checkpoint, Mapping):
            raise ValueError("checkpoint_payload_invalid")
        checkpoint = dict(checkpoint)
        versions = checkpoint.get("channel_versions") or {}
        channel_values: dict[str, Any] = {}
        for channel, version in versions.items():
            blob = connection.execute(
                """SELECT value_type, value_blob FROM bhm_langgraph_checkpoint_blobs
                   WHERE project=? AND caller_id=? AND thread_id=? AND checkpoint_ns=?
                     AND channel=? AND version=?""",
                (*self._scope(), row["thread_id"], row["checkpoint_ns"], str(channel), _version_key(version)),
            ).fetchone()
            if blob is None or str(blob["value_type"]) == "empty":
                continue
            channel_values[str(channel)] = self._load(blob["value_type"], blob["value_blob"])
        checkpoint["channel_values"] = channel_values
        pending_rows = connection.execute(
            """SELECT task_id, channel, value_type, value_blob FROM bhm_langgraph_checkpoint_writes
               WHERE project=? AND caller_id=? AND thread_id=? AND checkpoint_ns=? AND checkpoint_id=?
               ORDER BY task_id, task_path, write_index""",
            (*self._scope(), row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"]),
        ).fetchall()
        pending_writes = [
            (str(item["task_id"]), str(item["channel"]), self._load(item["value_type"], item["value_blob"]))
            for item in pending_rows
        ]
        parent_id = row["parent_checkpoint_id"]
        return CheckpointTuple(
            config=self._config(row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"]),
            checkpoint=checkpoint,
            metadata=metadata if isinstance(metadata, dict) else {},
            parent_config=(
                self._config(row["thread_id"], row["checkpoint_ns"], str(parent_id))
                if parent_id
                else None
            ),
            pending_writes=pending_writes,
        )

    def get_tuple(self, config: Mapping[str, Any]) -> CheckpointTuple | None:
        normalized, thread_id, checkpoint_ns, checkpoint_id = self._validate_config(config)
        del normalized
        connection = self._connect()
        try:
            if checkpoint_id is None:
                row = connection.execute(
                    """SELECT * FROM bhm_langgraph_checkpoints
                       WHERE project=? AND caller_id=? AND thread_id=? AND checkpoint_ns=?
                       ORDER BY checkpoint_id DESC LIMIT 1""",
                    (*self._scope(), thread_id, checkpoint_ns),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT * FROM bhm_langgraph_checkpoints
                       WHERE project=? AND caller_id=? AND thread_id=? AND checkpoint_ns=? AND checkpoint_id=?""",
                    (*self._scope(), thread_id, checkpoint_ns, checkpoint_id),
                ).fetchone()
            return self._checkpoint_row(connection, row) if row is not None else None
        finally:
            connection.close()

    def list(
        self,
        config: Mapping[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: Mapping[str, Any] | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        self._ensure_enabled()
        if limit is not None and limit <= 0:
            return
        if config is None:
            thread_id = checkpoint_ns = checkpoint_id = None
        else:
            _normalized, thread_id, checkpoint_ns, checkpoint_id = self._validate_config(config)
        before_id = None
        if before is not None:
            _normalized, before_thread, before_ns, before_id = self._validate_config(before)
            if thread_id is not None and (before_thread != thread_id or before_ns != checkpoint_ns):
                raise ValueError("before_scope_mismatch")
            if thread_id is None:
                thread_id, checkpoint_ns = before_thread, before_ns
        connection = self._connect()
        try:
            clauses = ["project=?", "caller_id=?"]
            params: list[Any] = [*self._scope()]
            if thread_id is not None:
                clauses.append("thread_id=?")
                params.append(thread_id)
            if checkpoint_ns is not None:
                clauses.append("checkpoint_ns=?")
                params.append(checkpoint_ns)
            if checkpoint_id is not None:
                clauses.append("checkpoint_id=?")
                params.append(checkpoint_id)
            if before_id is not None:
                clauses.append("checkpoint_id < ?")
                params.append(before_id)
            query = (
                "SELECT * FROM bhm_langgraph_checkpoints WHERE "
                + " AND ".join(clauses)
                + " ORDER BY checkpoint_id DESC"
            )
            yielded = 0
            for row in connection.execute(query, params):
                item = self._checkpoint_row(connection, row)
                if filter and not all(item.metadata.get(key) == value for key, value in filter.items()):
                    continue
                yield item
                yielded += 1
                if limit is not None and yielded >= limit:
                    break
        finally:
            connection.close()

    def put(
        self,
        config: Mapping[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> dict[str, Any]:
        normalized, thread_id, checkpoint_ns, parent_id = self._validate_config(config)
        checkpoint_id = _text(checkpoint.get("id"), name="checkpoint_id")
        if parent_id == checkpoint_id:
            raise ValueError("checkpoint_parent_cycle")
        values = checkpoint.get("channel_values") or {}
        if not isinstance(values, Mapping):
            raise ValueError("checkpoint_channel_values_invalid")
        checkpoint_core = dict(checkpoint)
        checkpoint_core.pop("channel_values", None)
        checkpoint_type, checkpoint_blob, checkpoint_digest = self._dump(checkpoint_core)
        clean_metadata = _redact(get_checkpoint_metadata(normalized, metadata))
        metadata_type, metadata_blob, metadata_digest = self._dump(clean_metadata)
        blob_rows: list[tuple[str, str, str, bytes, str]] = []
        total_size = len(checkpoint_blob) + len(metadata_blob)
        for channel, version in (new_versions or {}).items():
            if channel in values:
                value_type, value_blob, value_digest = self._dump(values[channel])
            else:
                value_type, value_blob, value_digest = "empty", b"", _digest(b"")
            blob_rows.append((str(channel), _version_key(version), value_type, value_blob, value_digest))
            total_size += len(value_blob)
        self._check_size(total_size, limit=self.max_state_bytes, error="checkpoint_state_too_large")

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if parent_id is not None:
                parent = connection.execute(
                    """SELECT 1 FROM bhm_langgraph_checkpoints
                       WHERE project=? AND caller_id=? AND thread_id=? AND checkpoint_ns=? AND checkpoint_id=?""",
                    (*self._scope(), thread_id, checkpoint_ns, parent_id),
                ).fetchone()
                if parent is None:
                    raise ValueError("checkpoint_parent_missing")
            created_at = _now_iso()
            connection.execute(
                """INSERT INTO bhm_langgraph_checkpoints(
                   project, caller_id, task_id, session_id, thread_id, checkpoint_ns,
                   checkpoint_id, parent_checkpoint_id, checkpoint_type, checkpoint_blob,
                   checkpoint_digest, metadata_type, metadata_blob, metadata_digest, run_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project, caller_id, thread_id, checkpoint_ns, checkpoint_id)
                DO UPDATE SET task_id=excluded.task_id, session_id=excluded.session_id,
                   parent_checkpoint_id=excluded.parent_checkpoint_id,
                   checkpoint_type=excluded.checkpoint_type, checkpoint_blob=excluded.checkpoint_blob,
                   checkpoint_digest=excluded.checkpoint_digest, metadata_type=excluded.metadata_type,
                   metadata_blob=excluded.metadata_blob, metadata_digest=excluded.metadata_digest,
                   run_id=excluded.run_id, created_at=excluded.created_at""",
                (
                    *self._scope(),
                    self.task_id,
                    self.session_id,
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                    parent_id,
                    checkpoint_type,
                    sqlite3.Binary(checkpoint_blob),
                    checkpoint_digest,
                    metadata_type,
                    sqlite3.Binary(metadata_blob),
                    metadata_digest,
                    normalized.get("configurable", {}).get("run_id"),
                    created_at,
                ),
            )
            for channel, version, value_type, value_blob, value_digest in blob_rows:
                connection.execute(
                    """INSERT INTO bhm_langgraph_checkpoint_blobs(
                       project, caller_id, thread_id, checkpoint_ns, channel, version,
                       value_type, value_blob, value_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project, caller_id, thread_id, checkpoint_ns, channel, version)
                    DO UPDATE SET value_type=excluded.value_type, value_blob=excluded.value_blob,
                       value_digest=excluded.value_digest""",
                    (*self._scope(), thread_id, checkpoint_ns, channel, version, value_type, sqlite3.Binary(value_blob), value_digest),
                )
            return self._config(thread_id, checkpoint_ns, checkpoint_id)

        return self._write_transaction(operation)

    def put_writes(
        self,
        config: Mapping[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        normalized, thread_id, checkpoint_ns, checkpoint_id = self._validate_config(
            config, require_checkpoint_id=True
        )
        del normalized
        write_task_id = _text(task_id, name="write_task_id")
        write_task_path = _text(task_path, name="task_path", required=False)
        serialized: list[tuple[str, int, str, bytes, str]] = []
        total = 0
        for index, (channel, value) in enumerate(writes):
            channel_name = _text(channel, name="write_channel")
            value_type, value_blob, value_digest = self._dump(value)
            self._check_size(len(value_blob), limit=self.max_write_bytes, error="checkpoint_write_too_large")
            total += len(value_blob)
            serialized.append((channel_name, WRITES_IDX_MAP.get(channel_name, index), value_type, value_blob, value_digest))
        self._check_size(total, limit=self.max_state_bytes, error="checkpoint_writes_too_large")

        def operation(connection: sqlite3.Connection) -> None:
            for channel, write_index, value_type, value_blob, value_digest in serialized:
                existing = connection.execute(
                    """SELECT value_digest FROM bhm_langgraph_checkpoint_writes
                       WHERE project=? AND caller_id=? AND thread_id=? AND checkpoint_ns=?
                         AND checkpoint_id=? AND task_id=? AND task_path=? AND channel=? AND write_index=?""",
                    (*self._scope(), thread_id, checkpoint_ns, checkpoint_id, write_task_id, write_task_path, channel, write_index),
                ).fetchone()
                if existing is not None:
                    if str(existing["value_digest"]) != value_digest:
                        raise ValueError("checkpoint_write_conflict")
                    continue
                connection.execute(
                    """INSERT INTO bhm_langgraph_checkpoint_writes(
                       project, caller_id, thread_id, checkpoint_ns, checkpoint_id,
                       task_id, task_path, channel, write_index, value_type, value_blob,
                       value_digest, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*self._scope(), thread_id, checkpoint_ns, checkpoint_id, write_task_id, write_task_path, channel, write_index, value_type, sqlite3.Binary(value_blob), value_digest, _now_iso()),
                )

        self._write_transaction(operation)

    def _delete_checkpoint_ids(
        self,
        connection: sqlite3.Connection,
        thread_id: str,
        ids: Sequence[tuple[str, str]],
    ) -> None:
        for checkpoint_ns, checkpoint_id in ids:
            connection.execute(
                """DELETE FROM bhm_langgraph_checkpoint_writes
                   WHERE project=? AND caller_id=? AND thread_id=? AND checkpoint_ns=? AND checkpoint_id=?""",
                (*self._scope(), thread_id, checkpoint_ns, checkpoint_id),
            )
            connection.execute(
                """DELETE FROM bhm_langgraph_checkpoints
                   WHERE project=? AND caller_id=? AND thread_id=? AND checkpoint_ns=? AND checkpoint_id=?""",
                (*self._scope(), thread_id, checkpoint_ns, checkpoint_id),
            )

    def delete_thread(self, thread_id: str) -> None:
        thread = _text(thread_id, name="thread_id")

        def operation(connection: sqlite3.Connection) -> None:
            scope = (*self._scope(), thread)
            connection.execute(
                "DELETE FROM bhm_langgraph_checkpoint_writes WHERE project=? AND caller_id=? AND thread_id=?",
                scope,
            )
            connection.execute(
                "DELETE FROM bhm_langgraph_checkpoint_blobs WHERE project=? AND caller_id=? AND thread_id=?",
                scope,
            )
            connection.execute(
                "DELETE FROM bhm_langgraph_checkpoints WHERE project=? AND caller_id=? AND thread_id=?",
                scope,
            )

        self._write_transaction(operation)

    def delete_for_runs(self, run_ids: Sequence[str]) -> None:
        normalized = [_text(run_id, name="run_id") for run_id in run_ids]
        if not normalized:
            return

        def operation(connection: sqlite3.Connection) -> None:
            for run_id in normalized:
                rows = connection.execute(
                    "SELECT DISTINCT thread_id, checkpoint_ns, checkpoint_id FROM bhm_langgraph_checkpoints WHERE project=? AND caller_id=? AND run_id=?",
                    (*self._scope(), run_id),
                ).fetchall()
                for row in rows:
                    self._delete_checkpoint_ids(
                        connection,
                        str(row["thread_id"]),
                        [(str(row["checkpoint_ns"]), str(row["checkpoint_id"]))],
                    )

        self._write_transaction(operation)

    def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        source = _text(source_thread_id, name="source_thread_id")
        target = _text(target_thread_id, name="target_thread_id")
        if source == target:
            raise ValueError("copy_thread_same_target")

        def operation(connection: sqlite3.Connection) -> None:
            existing = connection.execute(
                "SELECT 1 FROM bhm_langgraph_checkpoints WHERE project=? AND caller_id=? AND thread_id=? LIMIT 1",
                (*self._scope(), target),
            ).fetchone()
            if existing is not None:
                raise ValueError("copy_thread_target_not_empty")
            rows = connection.execute(
                "SELECT * FROM bhm_langgraph_checkpoints WHERE project=? AND caller_id=? AND thread_id=? ORDER BY checkpoint_id",
                (*self._scope(), source),
            ).fetchall()
            if not rows:
                raise ValueError("copy_thread_source_not_found")
            for row in rows:
                connection.execute(
                    """INSERT INTO bhm_langgraph_checkpoints(
                       project, caller_id, task_id, session_id, thread_id, checkpoint_ns,
                       checkpoint_id, parent_checkpoint_id, checkpoint_type, checkpoint_blob,
                       checkpoint_digest, metadata_type, metadata_blob, metadata_digest, run_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*self._scope(), row["task_id"], row["session_id"], target, row["checkpoint_ns"], row["checkpoint_id"], row["parent_checkpoint_id"], row["checkpoint_type"], row["checkpoint_blob"], row["checkpoint_digest"], row["metadata_type"], row["metadata_blob"], row["metadata_digest"], row["run_id"], row["created_at"]),
                )
            source_blobs = connection.execute(
                """SELECT checkpoint_ns, channel, version, value_type, value_blob, value_digest
                   FROM bhm_langgraph_checkpoint_blobs WHERE project=? AND caller_id=? AND thread_id=?""",
                (*self._scope(), source),
            ).fetchall()
            for item in source_blobs:
                connection.execute(
                    """INSERT INTO bhm_langgraph_checkpoint_blobs(
                       project, caller_id, thread_id, checkpoint_ns, channel, version,
                       value_type, value_blob, value_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*self._scope(), target, item["checkpoint_ns"], item["channel"], item["version"], item["value_type"], item["value_blob"], item["value_digest"]),
                )
            source_writes = connection.execute(
                """SELECT checkpoint_ns, checkpoint_id, task_id, task_path, channel,
                          write_index, value_type, value_blob, value_digest, created_at
                   FROM bhm_langgraph_checkpoint_writes WHERE project=? AND caller_id=? AND thread_id=?""",
                (*self._scope(), source),
            ).fetchall()
            for item in source_writes:
                connection.execute(
                    """INSERT INTO bhm_langgraph_checkpoint_writes(
                       project, caller_id, thread_id, checkpoint_ns, checkpoint_id,
                       task_id, task_path, channel, write_index, value_type, value_blob,
                       value_digest, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*self._scope(), target, item["checkpoint_ns"], item["checkpoint_id"], item["task_id"], item["task_path"], item["channel"], item["write_index"], item["value_type"], item["value_blob"], item["value_digest"], item["created_at"]),
                )

        self._write_transaction(operation)

    def prune(self, thread_ids: Sequence[str], *, strategy: str = "keep_latest") -> None:
        threads = [_text(thread_id, name="thread_id") for thread_id in thread_ids]
        if strategy not in {"keep_latest", "delete"}:
            raise ValueError("checkpoint_prune_strategy_invalid")

        def operation(connection: sqlite3.Connection) -> None:
            for thread in threads:
                if strategy == "delete":
                    scope = (*self._scope(), thread)
                    connection.execute("DELETE FROM bhm_langgraph_checkpoint_writes WHERE project=? AND caller_id=? AND thread_id=?", scope)
                    connection.execute("DELETE FROM bhm_langgraph_checkpoint_blobs WHERE project=? AND caller_id=? AND thread_id=?", scope)
                    connection.execute("DELETE FROM bhm_langgraph_checkpoints WHERE project=? AND caller_id=? AND thread_id=?", scope)
                    continue
                rows = connection.execute(
                    "SELECT checkpoint_ns, checkpoint_id, parent_checkpoint_id FROM bhm_langgraph_checkpoints WHERE project=? AND caller_id=? AND thread_id=?",
                    (*self._scope(), thread),
                ).fetchall()
                by_ns: dict[str, dict[str, str | None]] = {}
                for row in rows:
                    by_ns.setdefault(str(row["checkpoint_ns"]), {})[str(row["checkpoint_id"])] = row["parent_checkpoint_id"]
                keep: set[tuple[str, str]] = set()
                for checkpoint_ns, parents in by_ns.items():
                    if not parents:
                        continue
                    current = max(parents)
                    while current and (checkpoint_ns, current) not in keep:
                        keep.add((checkpoint_ns, current))
                        current = parents.get(current)
                delete_ids = [
                    (str(row["checkpoint_ns"]), str(row["checkpoint_id"]))
                    for row in rows
                    if (str(row["checkpoint_ns"]), str(row["checkpoint_id"])) not in keep
                ]
                self._delete_checkpoint_ids(connection, thread, delete_ids)

        self._write_transaction(operation)

    async def aget_tuple(self, config: Mapping[str, Any]) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: Mapping[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: Mapping[str, Any] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        items = await asyncio.to_thread(lambda: list(self.list(config, filter=filter, before=before, limit=limit)))
        for item in items:
            yield item

    async def aput(
        self,
        config: Mapping[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: Mapping[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)

    async def adelete_for_runs(self, run_ids: Sequence[str]) -> None:
        await asyncio.to_thread(self.delete_for_runs, run_ids)

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        await asyncio.to_thread(self.copy_thread, source_thread_id, target_thread_id)

    async def aprune(self, thread_ids: Sequence[str], *, strategy: str = "keep_latest") -> None:
        await asyncio.to_thread(self.prune, thread_ids, strategy=strategy)


__all__ = ["CHECKPOINT_SCHEMA_VERSION", "SQLiteLangGraphCheckpointSaver"]
