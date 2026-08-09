"""Durable, bounded priority queue for local-LLM work.

The queue is deliberately independent from execution and from the LLM gateway:
it persists job intent and lifecycle, but never calls a model or applies output.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .filesystem_boundaries import assert_safe_path


LLM_JOB_QUEUE_SCHEMA_VERSION = 1
LLM_JOB_QUEUE_BUSY_TIMEOUT_MS = 5_000
LLM_JOB_QUEUE_WRITE_RETRY_DELAYS = (0.025, 0.05, 0.1, 0.2, 0.4)
LLM_JOB_QUEUE_MAX_PAYLOAD_BYTES = 256 * 1024
LLM_JOB_QUEUE_MAX_RESULT_BYTES = 64 * 1024
LLM_JOB_QUEUE_MAX_CHECKPOINT_BYTES = 32 * 1024

LLMJobStatus = Literal["queued", "processing", "completed", "failed", "dead_letter", "cancelled"]


class LLMJobQueueError(RuntimeError):
    """Base error for durable LLM queue operations."""


class LLMJobQueueFull(LLMJobQueueError):
    def __init__(self, *, pending: int, capacity: int) -> None:
        self.pending = pending
        self.capacity = capacity
        super().__init__(f"LLM job queue full: {pending}/{capacity}")


class LLMJobIdempotencyCollision(LLMJobQueueError):
    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(f"LLM job idempotency collision: {idempotency_key}")


class LLMJobLeaseLost(LLMJobQueueError):
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"LLM job lease lost: {job_id}")


@dataclass(frozen=True)
class LLMJobEnqueueResult:
    job_id: str
    idempotency_key: str
    status: LLMJobStatus
    inserted: bool
    pending: int
    capacity: int
    created_at: str


@dataclass(frozen=True)
class LLMJobCheckpoint:
    job_id: str
    digest: str
    updated_at: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
        allow_nan=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_IDEMPOTENCY_SEPARATOR = "\x1f"


def _scoped_idempotency_key(project: str, idempotency_key: str) -> str:
    return f"{project}{_IDEMPOTENCY_SEPARATOR}{idempotency_key}"


def _public_idempotency_key(value: str) -> str:
    return str(value).split(_IDEMPOTENCY_SEPARATOR, 1)[-1]


def _job_id(idempotency_key: str, project: str = "blackholememory") -> str:
    return f"llmjob_{_sha256(_scoped_idempotency_key(project, idempotency_key))[:32]}"


def deterministic_llm_job_id(idempotency_key: str, project: str = "blackholememory") -> str:
    normalized = str(idempotency_key or "").strip()
    if not normalized:
        raise LLMJobQueueError("idempotency_key is required")
    normalized_project = str(project or "").strip() or "blackholememory"
    return _job_id(normalized, normalized_project)


def default_llm_job_queue_path() -> Path:
    configured = str(os.getenv("BHM_LLM_JOB_QUEUE_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / ".runtime" / "llm-jobs" / "queue.sqlite3"


class LLMJobQueue:
    """Durable priority queue with explicit pause, lease and recovery semantics."""

    def __init__(self, path: Path | str, *, capacity: int = 128) -> None:
        self.path = Path(path)
        self.capacity = max(int(capacity), 1)
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            assert_safe_path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            assert_safe_path(self.path.parent, reject_hardlink_target=False)

            def initialize_schema() -> None:
                assert_safe_path(self.path)
                with closing(self._connect()) as connection:
                    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if current_version not in {0, LLM_JOB_QUEUE_SCHEMA_VERSION}:
                        raise LLMJobQueueError(
                            f"unsupported LLM job queue schema {current_version}; "
                            f"expected {LLM_JOB_QUEUE_SCHEMA_VERSION}"
                        )
                    journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold()
                    if journal_mode != "wal":
                        raise LLMJobQueueError(f"SQLite refused WAL mode for {self.path}: {journal_mode}")
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS llm_queue_meta (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS llm_jobs (
                            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                            job_id TEXT NOT NULL UNIQUE,
                            idempotency_key TEXT NOT NULL UNIQUE,
                            job_type TEXT NOT NULL,
                            project TEXT NOT NULL,
                            priority INTEGER NOT NULL,
                            payload_json TEXT NOT NULL,
                            payload_sha256 TEXT NOT NULL,
                            status TEXT NOT NULL CHECK (
                                status IN ('queued', 'processing', 'completed', 'failed', 'dead_letter', 'cancelled')
                            ),
                            attempts INTEGER NOT NULL DEFAULT 0,
                            max_attempts INTEGER NOT NULL,
                            available_at REAL NOT NULL,
                            lease_owner TEXT,
                            lease_expires_at REAL,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            started_at TEXT,
                            finished_at TEXT,
                            last_error TEXT,
                            result_json TEXT,
                            checkpoint_json TEXT,
                            checkpoint_digest TEXT
                        );

                        CREATE INDEX IF NOT EXISTS idx_llm_jobs_claim
                            ON llm_jobs(status, available_at, priority, sequence);
                        CREATE INDEX IF NOT EXISTS idx_llm_jobs_project
                            ON llm_jobs(project, created_at);
                        CREATE INDEX IF NOT EXISTS idx_llm_jobs_type
                            ON llm_jobs(job_type, status, priority, sequence);
                        """
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO llm_queue_meta(key, value) VALUES (?, ?)",
                        ("schema_version", str(LLM_JOB_QUEUE_SCHEMA_VERSION)),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO llm_queue_meta(key, value) VALUES (?, ?)",
                        ("created_at", _utc_now_iso()),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO llm_queue_meta(key, value) VALUES (?, ?)",
                        ("paused", "0"),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO llm_queue_meta(key, value) VALUES (?, ?)",
                        ("pause_reason", ""),
                    )
                    connection.execute(f"PRAGMA user_version={LLM_JOB_QUEUE_SCHEMA_VERSION}")
                    connection.execute("PRAGMA wal_autocheckpoint=1000")

            self._with_write_retry(initialize_schema)
            self._initialized = True

    def enqueue(
        self,
        *,
        idempotency_key: str,
        job_type: str,
        payload: dict[str, Any],
        project: str = "blackholememory",
        priority: int = 100,
        max_attempts: int = 3,
        available_at: float | None = None,
    ) -> LLMJobEnqueueResult:
        normalized_key = str(idempotency_key or "").strip()
        normalized_type = str(job_type or "").strip()
        normalized_project = str(project or "").strip() or "blackholememory"
        if not normalized_key:
            raise LLMJobQueueError("idempotency_key is required")
        if not normalized_type:
            raise LLMJobQueueError("job_type is required")
        if not isinstance(payload, dict):
            raise LLMJobQueueError("LLM job payload must be an object")
        payload_json = _canonical_json(payload)
        payload_size = len(payload_json.encode("utf-8"))
        if payload_size > LLM_JOB_QUEUE_MAX_PAYLOAD_BYTES:
            raise LLMJobQueueError("LLM job payload exceeds durable payload limit")
        payload_sha256 = _sha256(payload_json)
        storage_key = _scoped_idempotency_key(normalized_project, normalized_key)
        job_id = _job_id(normalized_key, normalized_project)
        created_at = _utc_now_iso()
        available_epoch = time.time() if available_at is None else float(available_at)
        attempts_limit = max(int(max_attempts), 1)
        self.initialize()

        def write() -> LLMJobEnqueueResult:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT job_id, idempotency_key, job_type, payload_sha256, status, created_at
                    FROM llm_jobs
                    WHERE idempotency_key = ?
                       OR (idempotency_key = ? AND project = ?)
                    """,
                    (storage_key, normalized_key, normalized_project),
                ).fetchone()
                pending = self._pending_count_connection(connection)
                if existing is not None:
                    if str(existing["job_type"]) != normalized_type or str(existing["payload_sha256"]) != payload_sha256:
                        raise LLMJobIdempotencyCollision(normalized_key)
                    connection.commit()
                    return LLMJobEnqueueResult(
                        job_id=str(existing["job_id"]),
                        idempotency_key=normalized_key,
                        status=str(existing["status"]),
                        inserted=False,
                        pending=pending,
                        capacity=self.capacity,
                        created_at=str(existing["created_at"]),
                    )
                if pending >= self.capacity:
                    raise LLMJobQueueFull(pending=pending, capacity=self.capacity)
                connection.execute(
                    """
                    INSERT INTO llm_jobs(
                        job_id, idempotency_key, job_type, project, priority,
                        payload_json, payload_sha256, status, attempts, max_attempts,
                        available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        storage_key,
                        normalized_type,
                        normalized_project,
                        int(priority),
                        payload_json,
                        payload_sha256,
                        attempts_limit,
                        available_epoch,
                        created_at,
                        created_at,
                    ),
                )
                connection.commit()
                return LLMJobEnqueueResult(
                    job_id=job_id,
                    idempotency_key=normalized_key,
                    status="queued",
                    inserted=True,
                    pending=pending + 1,
                    capacity=self.capacity,
                    created_at=created_at,
                )
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write, priority=True)

    def claim_next(
        self,
        *,
        owner: str,
        lease_seconds: float,
        job_types: tuple[str, ...] | list[str] | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        normalized_owner = str(owner or "").strip()
        if not normalized_owner:
            raise LLMJobQueueError("lease owner is required")
        self.initialize()
        now_epoch = time.time() if now is None else float(now)
        lease_expires_at = now_epoch + max(float(lease_seconds), 1.0)
        now_iso = _utc_now_iso()
        normalized_types = tuple(dict.fromkeys(str(item).strip() for item in (job_types or []) if str(item).strip()))

        def write() -> dict[str, Any] | None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE llm_jobs
                    SET status = 'queued',
                        available_at = ?,
                        updated_at = ?,
                        last_error = 'processing lease expired; requeued',
                        lease_owner = NULL,
                        lease_expires_at = NULL
                    WHERE status = 'processing'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                    """,
                    (now_epoch, now_iso, now_epoch),
                )
                if self._is_paused_connection(connection):
                    connection.commit()
                    return None
                clauses = ["status = 'queued'", "available_at <= ?"]
                params: list[Any] = [now_epoch]
                if normalized_types:
                    placeholders = ",".join("?" for _ in normalized_types)
                    clauses.append(f"job_type IN ({placeholders})")
                    params.extend(normalized_types)
                row = connection.execute(
                    f"""
                    SELECT * FROM llm_jobs
                    WHERE {' AND '.join(clauses)}
                    ORDER BY priority ASC, sequence ASC
                    LIMIT 1
                    """,
                    params,
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                cursor = connection.execute(
                    """
                    UPDATE llm_jobs
                    SET status = 'processing',
                        attempts = attempts + 1,
                        lease_owner = ?,
                        lease_expires_at = ?,
                        started_at = COALESCE(started_at, ?),
                        updated_at = ?
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (normalized_owner, lease_expires_at, now_iso, now_iso, str(row["job_id"])),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                claimed = connection.execute(
                    "SELECT * FROM llm_jobs WHERE job_id = ?",
                    (str(row["job_id"]),),
                ).fetchone()
                connection.commit()
                return self._materialize_job(claimed, include_payload=True)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write)

    def renew_lease(self, job_id: str, *, owner: str, lease_seconds: float) -> bool:
        self.initialize()
        expires_at = time.time() + max(float(lease_seconds), 1.0)

        def write() -> bool:
            with closing(self._connect()) as connection:
                cursor = connection.execute(
                    """
                    UPDATE llm_jobs
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'processing' AND lease_owner = ?
                    """,
                    (expires_at, _utc_now_iso(), str(job_id), str(owner)),
                )
                return cursor.rowcount == 1

        return self._with_write_retry(write)

    def complete(
        self,
        job_id: str,
        *,
        owner: str,
        result: dict[str, Any],
        checkpoint: dict[str, Any] | None = None,
    ) -> None:
        result_json = _canonical_json(result)
        if len(result_json.encode("utf-8")) > LLM_JOB_QUEUE_MAX_RESULT_BYTES:
            raise LLMJobQueueError("LLM job result exceeds durable result limit")
        checkpoint_json, checkpoint_digest = self._checkpoint_values(checkpoint)
        self.initialize()

        def write() -> None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = _utc_now_iso()
                cursor = connection.execute(
                    """
                    UPDATE llm_jobs
                    SET status = 'completed',
                        finished_at = ?,
                        updated_at = ?,
                        result_json = ?,
                        checkpoint_json = COALESCE(?, checkpoint_json),
                        checkpoint_digest = COALESCE(?, checkpoint_digest),
                        last_error = NULL,
                        lease_owner = NULL,
                        lease_expires_at = NULL
                    WHERE job_id = ? AND status = 'processing' AND lease_owner = ?
                    """,
                    (now, now, result_json, checkpoint_json, checkpoint_digest, str(job_id), str(owner)),
                )
                if cursor.rowcount != 1:
                    raise LLMJobLeaseLost(str(job_id))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        self._with_write_retry(write)

    def fail(
        self,
        job_id: str,
        *,
        owner: str,
        error: str,
        retry_delay_seconds: float = 0.0,
        retryable: bool = True,
    ) -> LLMJobStatus:
        self.initialize()

        def write() -> LLMJobStatus:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT attempts, max_attempts
                    FROM llm_jobs
                    WHERE job_id = ? AND status = 'processing' AND lease_owner = ?
                    """,
                    (str(job_id), str(owner)),
                ).fetchone()
                if row is None:
                    raise LLMJobLeaseLost(str(job_id))
                should_retry = bool(retryable) and int(row["attempts"]) < int(row["max_attempts"])
                status: LLMJobStatus = "queued" if should_retry else ("dead_letter" if retryable else "failed")
                now = _utc_now_iso()
                available_at = time.time() + max(float(retry_delay_seconds), 0.0) if should_retry else time.time()
                connection.execute(
                    """
                    UPDATE llm_jobs
                    SET status = ?,
                        available_at = ?,
                        updated_at = ?,
                        finished_at = CASE WHEN ? IN ('failed', 'dead_letter') THEN ? ELSE NULL END,
                        last_error = ?,
                        lease_owner = NULL,
                        lease_expires_at = NULL
                    WHERE job_id = ?
                    """,
                    (status, available_at, now, status, now, str(error)[:4_000], str(job_id)),
                )
                connection.commit()
                return status
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write)

    def cancel(self, job_id: str, *, reason: str = "cancelled by operator") -> dict[str, Any] | None:
        self.initialize()

        def write() -> dict[str, Any] | None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM llm_jobs WHERE job_id = ?", (str(job_id),)).fetchone()
                if row is None:
                    connection.commit()
                    return None
                if str(row["status"]) in {"queued", "processing"}:
                    now = _utc_now_iso()
                    connection.execute(
                        """
                        UPDATE llm_jobs
                        SET status = 'cancelled', finished_at = ?, updated_at = ?,
                            last_error = ?, lease_owner = NULL, lease_expires_at = NULL
                        WHERE job_id = ? AND status IN ('queued', 'processing')
                        """,
                        (now, now, str(reason)[:4_000], str(job_id)),
                    )
                updated = connection.execute("SELECT * FROM llm_jobs WHERE job_id = ?", (str(job_id),)).fetchone()
                connection.commit()
                return self._materialize_job(updated, include_payload=False)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write)

    def pause(self, *, reason: str = "paused by operator") -> dict[str, Any]:
        self.initialize()
        normalized_reason = str(reason or "paused by operator")[:1_000]

        def write() -> dict[str, Any]:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE llm_queue_meta SET value = '1' WHERE key = 'paused'")
                connection.execute(
                    "UPDATE llm_queue_meta SET value = ? WHERE key = 'pause_reason'",
                    (normalized_reason,),
                )
                connection.commit()
            return self.status()

        return self._with_write_retry(write)

    def resume(self) -> dict[str, Any]:
        self.initialize()

        def write() -> dict[str, Any]:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE llm_queue_meta SET value = '0' WHERE key = 'paused'")
                connection.execute("UPDATE llm_queue_meta SET value = '' WHERE key = 'pause_reason'")
                connection.commit()
            return self.status()

        return self._with_write_retry(write)

    def recover_processing(self, *, reason: str = "runtime restart recovery") -> int:
        self.initialize()

        def write() -> int:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE llm_jobs
                    SET status = 'queued', available_at = ?, updated_at = ?, last_error = ?,
                        lease_owner = NULL, lease_expires_at = NULL
                    WHERE status = 'processing'
                    """,
                    (time.time(), _utc_now_iso(), str(reason)[:4_000]),
                )
                connection.commit()
                return int(cursor.rowcount)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write)

    def checkpoint(
        self,
        job_id: str,
        *,
        owner: str,
        data: dict[str, Any],
    ) -> LLMJobCheckpoint:
        checkpoint_json, digest = self._checkpoint_values(data)
        assert checkpoint_json is not None
        self.initialize()

        def write() -> LLMJobCheckpoint:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = _utc_now_iso()
                cursor = connection.execute(
                    """
                    UPDATE llm_jobs
                    SET checkpoint_json = ?, checkpoint_digest = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'processing' AND lease_owner = ?
                    """,
                    (checkpoint_json, digest, now, str(job_id), str(owner)),
                )
                if cursor.rowcount != 1:
                    raise LLMJobLeaseLost(str(job_id))
                connection.commit()
                return LLMJobCheckpoint(job_id=str(job_id), digest=str(digest), updated_at=now)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write)

    def get(self, job_id: str, *, include_payload: bool = False) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM llm_jobs WHERE job_id = ?", (str(job_id),)).fetchone()
        return self._materialize_job(row, include_payload=include_payload) if row is not None else None

    def list(self, *, status: LLMJobStatus | None = None, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        params: list[Any] = []
        where = ""
        if status:
            where = "WHERE status = ?"
            params.append(status)
        params.append(max(min(int(limit), 1_000), 1))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM llm_jobs {where} ORDER BY sequence ASC LIMIT ?",
                params,
            ).fetchall()
        return [self._materialize_job(row, include_payload=False) for row in rows]

    def status(self) -> dict[str, Any]:
        self.initialize()
        with closing(self._connect()) as connection:
            counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute("SELECT status, COUNT(*) AS count FROM llm_jobs GROUP BY status")
            }
            meta = {
                str(row["key"]): str(row["value"])
                for row in connection.execute("SELECT key, value FROM llm_queue_meta")
            }
        return {
            "schema_version": LLM_JOB_QUEUE_SCHEMA_VERSION,
            "path": str(self.path),
            "capacity": self.capacity,
            "paused": meta.get("paused") == "1",
            "pause_reason": meta.get("pause_reason", ""),
            "counts": {status: counts.get(status, 0) for status in ("queued", "processing", "completed", "failed", "dead_letter", "cancelled")},
            "pending": counts.get("queued", 0) + counts.get("processing", 0),
        }

    def _connect(self) -> sqlite3.Connection:
        assert_safe_path(self.path)
        connection = sqlite3.connect(
            self.path,
            timeout=LLM_JOB_QUEUE_BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={LLM_JOB_QUEUE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _pending_count_connection(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM llm_jobs WHERE status IN ('queued', 'processing')"
        ).fetchone()
        return int(row["count"])

    def _is_paused_connection(self, connection: sqlite3.Connection) -> bool:
        row = connection.execute("SELECT value FROM llm_queue_meta WHERE key = 'paused'").fetchone()
        return bool(row and str(row["value"]) == "1")

    @staticmethod
    def _checkpoint_values(data: dict[str, Any] | None) -> tuple[str | None, str | None]:
        if data is None:
            return None, None
        if not isinstance(data, dict):
            raise LLMJobQueueError("checkpoint must be an object")
        checkpoint_json = _canonical_json(data)
        if len(checkpoint_json.encode("utf-8")) > LLM_JOB_QUEUE_MAX_CHECKPOINT_BYTES:
            raise LLMJobQueueError("LLM job checkpoint exceeds durable checkpoint limit")
        return checkpoint_json, _sha256(checkpoint_json)

    @staticmethod
    def _materialize_job(row: sqlite3.Row, *, include_payload: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "job_id": str(row["job_id"]),
            "idempotency_key": _public_idempotency_key(str(row["idempotency_key"])),
            "job_type": str(row["job_type"]),
            "project": str(row["project"]),
            "priority": int(row["priority"]),
            "payload_sha256": str(row["payload_sha256"]),
            "status": str(row["status"]),
            "attempts": int(row["attempts"]),
            "max_attempts": int(row["max_attempts"]),
            "available_at": float(row["available_at"]),
            "lease_owner": str(row["lease_owner"]) if row["lease_owner"] else None,
            "lease_expires_at": float(row["lease_expires_at"]) if row["lease_expires_at"] else None,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "started_at": str(row["started_at"]) if row["started_at"] else None,
            "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
            "last_error": str(row["last_error"]) if row["last_error"] else None,
            "result": json.loads(str(row["result_json"])) if row["result_json"] else None,
            "checkpoint": json.loads(str(row["checkpoint_json"])) if row["checkpoint_json"] else None,
            "checkpoint_digest": str(row["checkpoint_digest"]) if row["checkpoint_digest"] else None,
        }
        if include_payload:
            result["payload"] = json.loads(str(row["payload_json"]))
        return result

    def _with_write_retry(self, operation, *, priority: bool = False):
        delays = (0.0,) + LLM_JOB_QUEUE_WRITE_RETRY_DELAYS
        if priority:
            delays = (0.0, 0.0) + LLM_JOB_QUEUE_WRITE_RETRY_DELAYS
        last_error: Exception | None = None
        for delay in delays:
            if delay:
                time.sleep(delay)
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                message = str(exc).casefold()
                if "locked" not in message and "busy" not in message:
                    raise
                last_error = exc
        raise LLMJobQueueError("SQLite remained locked during LLM job queue write") from last_error


__all__ = [
    "LLM_JOB_QUEUE_MAX_CHECKPOINT_BYTES",
    "LLM_JOB_QUEUE_MAX_PAYLOAD_BYTES",
    "LLM_JOB_QUEUE_MAX_RESULT_BYTES",
    "LLM_JOB_QUEUE_SCHEMA_VERSION",
    "LLMJobCheckpoint",
    "LLMJobEnqueueResult",
    "LLMJobIdempotencyCollision",
    "LLMJobLeaseLost",
    "LLMJobQueue",
    "LLMJobQueueError",
    "LLMJobQueueFull",
    "default_llm_job_queue_path",
    "deterministic_llm_job_id",
]
