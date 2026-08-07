from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from .filesystem_boundaries import assert_safe_path


HOOK_QUEUE_SCHEMA_VERSION = 2
HOOK_QUEUE_BUSY_TIMEOUT_MS = 5_000
HOOK_QUEUE_WRITE_RETRY_DELAYS = (0.025, 0.05, 0.1, 0.2, 0.4)
HOOK_QUEUE_MAX_RESULT_BYTES = 64 * 1024
HOOK_QUEUE_ENQUEUE_PRIORITY_HEAD_START_SECONDS = 0.01
HookJobKind = Literal["compact", "idle"]
HookJobStatus = Literal["queued", "processing", "completed", "failed"]


class HookQueueError(RuntimeError):
    pass


class HookQueueFull(HookQueueError):
    def __init__(self, *, pending: int, capacity: int) -> None:
        self.pending = pending
        self.capacity = capacity
        super().__init__(f"hook queue full: {pending}/{capacity}")


class HookJobCollision(HookQueueError):
    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        super().__init__(f"hook job event id collision: {event_id}")


class HookJobLeaseLost(HookQueueError):
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"hook job lease lost: {job_id}")


@dataclass(frozen=True)
class HookEnqueueResult:
    job_id: str
    event_id: str
    status: HookJobStatus
    inserted: bool
    pending: int
    capacity: int
    created_at: str


@dataclass(frozen=True)
class HookExpireResult:
    expired: int
    payload_bytes: int
    result_bytes: int
    job_ids: tuple[str, ...]
    checkpoint: str


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


def _job_id(kind: HookJobKind, event_id: str) -> str:
    digest = hashlib.sha256(f"{kind}:{event_id}".encode("utf-8")).hexdigest()[:32]
    return f"hookjob_{digest}"


class HookJobQueue:
    """Durable bounded inbox for compact and idle hook processing."""

    def __init__(self, path: Path | str, *, capacity: int = 128) -> None:
        self.path = Path(path)
        self.capacity = max(int(capacity), 1)
        self._initialize_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._priority_lock = threading.Lock()
        self._enqueue_waiters = 0
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)

            def initialize_schema() -> None:
                with closing(self._connect()) as connection:
                    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if current_version not in {0, 1, HOOK_QUEUE_SCHEMA_VERSION}:
                        raise HookQueueError(
                            f"unsupported hook queue schema {current_version}; expected {HOOK_QUEUE_SCHEMA_VERSION}"
                        )
                    journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold()
                    if journal_mode != "wal":
                        raise HookQueueError(f"SQLite refused WAL mode for {self.path}: {journal_mode}")
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS hook_queue_meta (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS hook_jobs (
                            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                            job_id TEXT NOT NULL UNIQUE,
                            event_id TEXT NOT NULL UNIQUE,
                            kind TEXT NOT NULL CHECK (kind IN ('compact', 'idle')),
                            hook_type TEXT NOT NULL,
                            session_id TEXT NOT NULL,
                            project TEXT NOT NULL,
                            priority INTEGER NOT NULL,
                            payload_json TEXT NOT NULL,
                            payload_sha256 TEXT NOT NULL,
                            status TEXT NOT NULL CHECK (status IN ('queued', 'processing', 'completed', 'failed')),
                            attempts INTEGER NOT NULL DEFAULT 0,
                            max_attempts INTEGER NOT NULL,
                            available_at REAL NOT NULL,
                            lease_owner TEXT,
                            lease_expires_at REAL,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            started_at TEXT,
                            completed_at TEXT,
                            last_error TEXT,
                            result_json TEXT
                        );

                        CREATE INDEX IF NOT EXISTS idx_hook_jobs_claim
                            ON hook_jobs(status, kind, available_at, priority, sequence);
                        CREATE INDEX IF NOT EXISTS idx_hook_jobs_project
                            ON hook_jobs(project, created_at);
                        CREATE INDEX IF NOT EXISTS idx_hook_jobs_session
                            ON hook_jobs(session_id, created_at);

                        CREATE TABLE IF NOT EXISTS hook_job_tombstones (
                            job_id TEXT PRIMARY KEY,
                            event_id TEXT NOT NULL UNIQUE,
                            kind TEXT NOT NULL CHECK (kind IN ('compact', 'idle')),
                            hook_type TEXT NOT NULL,
                            project TEXT NOT NULL,
                            payload_sha256 TEXT NOT NULL,
                            final_status TEXT NOT NULL CHECK (final_status IN ('completed', 'failed')),
                            attempts INTEGER NOT NULL,
                            created_at TEXT NOT NULL,
                            completed_at TEXT NOT NULL,
                            payload_bytes INTEGER NOT NULL,
                            result_bytes INTEGER NOT NULL,
                            purged_at TEXT NOT NULL,
                            purge_reason TEXT,
                            policy_name TEXT
                        );

                        CREATE INDEX IF NOT EXISTS idx_hook_job_tombstones_policy_time
                            ON hook_job_tombstones(policy_name, purged_at);
                        CREATE INDEX IF NOT EXISTS idx_hook_job_tombstones_status_time
                            ON hook_job_tombstones(final_status, purged_at);
                        """
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO hook_queue_meta(key, value) VALUES (?, ?)",
                        ("schema_version", str(HOOK_QUEUE_SCHEMA_VERSION)),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO hook_queue_meta(key, value) VALUES (?, ?)",
                        ("created_at", _utc_now_iso()),
                    )
                    connection.execute(f"PRAGMA user_version={HOOK_QUEUE_SCHEMA_VERSION}")
                    connection.execute("PRAGMA wal_autocheckpoint=1000")

            self._with_write_retry(initialize_schema)
            self._initialized = True

    def enqueue(
        self,
        kind: HookJobKind,
        payload: dict[str, Any],
        *,
        priority: int,
        max_attempts: int = 3,
    ) -> HookEnqueueResult:
        if kind not in {"compact", "idle"}:
            raise HookQueueError(f"unsupported hook job kind: {kind}")
        event_id = str(payload.get("eventId") or "").strip()
        if not event_id:
            raise HookQueueError("durable hook job requires eventId")
        payload_json = _canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        job_id = _job_id(kind, event_id)
        created_at = _utc_now_iso()
        now_epoch = time.time()
        self.initialize()

        def write() -> HookEnqueueResult:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT job_id, event_id, kind, payload_sha256, status, created_at
                    FROM hook_jobs
                    WHERE event_id = ?
                    """,
                    (event_id,),
                ).fetchone()
                pending = self._pending_count_connection(connection)
                if existing is not None:
                    if str(existing["kind"]) != kind or str(existing["payload_sha256"]) != payload_sha256:
                        raise HookJobCollision(event_id)
                    connection.commit()
                    return HookEnqueueResult(
                        job_id=str(existing["job_id"]),
                        event_id=event_id,
                        status=str(existing["status"]),
                        inserted=False,
                        pending=pending,
                        capacity=self.capacity,
                        created_at=str(existing["created_at"]),
                    )
                tombstone = connection.execute(
                    """
                    SELECT job_id, event_id, kind, payload_sha256, final_status, created_at
                    FROM hook_job_tombstones
                    WHERE event_id = ?
                    """,
                    (event_id,),
                ).fetchone()
                if tombstone is not None:
                    if str(tombstone["kind"]) != kind or str(tombstone["payload_sha256"]) != payload_sha256:
                        raise HookJobCollision(event_id)
                    connection.commit()
                    return HookEnqueueResult(
                        job_id=str(tombstone["job_id"]),
                        event_id=event_id,
                        status=str(tombstone["final_status"]),
                        inserted=False,
                        pending=pending,
                        capacity=self.capacity,
                        created_at=str(tombstone["created_at"]),
                    )
                if pending >= self.capacity:
                    raise HookQueueFull(pending=pending, capacity=self.capacity)

                connection.execute(
                    """
                    INSERT INTO hook_jobs(
                        job_id,
                        event_id,
                        kind,
                        hook_type,
                        session_id,
                        project,
                        priority,
                        payload_json,
                        payload_sha256,
                        status,
                        attempts,
                        max_attempts,
                        available_at,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        event_id,
                        kind,
                        str(payload.get("hookType") or kind),
                        str(payload.get("sessionId") or event_id),
                        str(payload.get("project") or "e-github-workspace"),
                        int(priority),
                        payload_json,
                        payload_sha256,
                        max(int(max_attempts), 1),
                        now_epoch,
                        created_at,
                        created_at,
                    ),
                )
                connection.commit()
                return HookEnqueueResult(
                    job_id=job_id,
                    event_id=event_id,
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
        kinds: Sequence[HookJobKind],
        owner: str,
        lease_seconds: float,
    ) -> dict[str, Any] | None:
        normalized_kinds = tuple(dict.fromkeys(kind for kind in kinds if kind in {"compact", "idle"}))
        if not normalized_kinds:
            return None
        self.initialize()
        now_epoch = time.time()
        lease_expires_at = now_epoch + max(float(lease_seconds), 1.0)
        now_iso = _utc_now_iso()

        def write() -> dict[str, Any] | None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE hook_jobs
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
                placeholders = ",".join("?" for _ in normalized_kinds)
                row = connection.execute(
                    f"""
                    SELECT *
                    FROM hook_jobs
                    WHERE status = 'queued'
                      AND available_at <= ?
                      AND kind IN ({placeholders})
                    ORDER BY priority ASC, sequence ASC
                    LIMIT 1
                    """,
                    (now_epoch, *normalized_kinds),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                cursor = connection.execute(
                    """
                    UPDATE hook_jobs
                    SET status = 'processing',
                        attempts = attempts + 1,
                        lease_owner = ?,
                        lease_expires_at = ?,
                        started_at = COALESCE(started_at, ?),
                        updated_at = ?
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (owner, lease_expires_at, now_iso, now_iso, str(row["job_id"])),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                claimed = connection.execute(
                    "SELECT * FROM hook_jobs WHERE job_id = ?",
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
        lease_expires_at = time.time() + max(float(lease_seconds), 1.0)
        def write() -> bool:
            with closing(self._connect()) as connection:
                cursor = connection.execute(
                    """
                    UPDATE hook_jobs
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'processing' AND lease_owner = ?
                    """,
                    (lease_expires_at, _utc_now_iso(), job_id, owner),
                )
            return cursor.rowcount == 1

        return self._with_write_retry(write)

    def complete(self, job_id: str, *, owner: str, result: dict[str, Any]) -> None:
        result_json = _canonical_json(result)
        if len(result_json.encode("utf-8")) > HOOK_QUEUE_MAX_RESULT_BYTES:
            raise HookQueueError("hook job result exceeds durable result limit")
        self.initialize()

        def write() -> None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE hook_jobs
                    SET status = 'completed',
                        completed_at = ?,
                        updated_at = ?,
                        result_json = ?,
                        last_error = NULL,
                        lease_owner = NULL,
                        lease_expires_at = NULL
                    WHERE job_id = ? AND status = 'processing' AND lease_owner = ?
                    """,
                    (_utc_now_iso(), _utc_now_iso(), result_json, job_id, owner),
                )
                if cursor.rowcount != 1:
                    raise HookJobLeaseLost(job_id)
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
        retry_delay_seconds: float,
    ) -> HookJobStatus:
        self.initialize()

        def write() -> HookJobStatus:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT attempts, max_attempts FROM hook_jobs WHERE job_id = ? AND status = 'processing' AND lease_owner = ?",
                    (job_id, owner),
                ).fetchone()
                if row is None:
                    raise HookJobLeaseLost(job_id)
                retry = int(row["attempts"]) < int(row["max_attempts"])
                status: HookJobStatus = "queued" if retry else "failed"
                available_at = time.time() + max(float(retry_delay_seconds), 0.0) if retry else time.time()
                now = _utc_now_iso()
                connection.execute(
                    """
                    UPDATE hook_jobs
                    SET status = ?,
                        available_at = ?,
                        updated_at = ?,
                        completed_at = CASE WHEN ? = 'failed' THEN ? ELSE NULL END,
                        last_error = ?,
                        lease_owner = NULL,
                        lease_expires_at = NULL
                    WHERE job_id = ?
                    """,
                    (status, available_at, now, status, now, str(error)[:4000], job_id),
                )
                connection.commit()
                return status
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write)

    def recover_processing(self, *, reason: str = "runtime restart recovery") -> int:
        self.initialize()

        def write() -> int:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE hook_jobs
                    SET status = 'queued',
                        available_at = ?,
                        updated_at = ?,
                        last_error = ?,
                        lease_owner = NULL,
                        lease_expires_at = NULL
                    WHERE status = 'processing'
                    """,
                    (time.time(), _utc_now_iso(), reason[:4000]),
                )
                connection.commit()
                return int(cursor.rowcount)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write)

    def retention_candidates(self, *, project: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        self.initialize()
        params: list[Any] = []
        clauses = ["status IN ('completed', 'failed')"]
        if project:
            clauses.append("project = ?")
            params.append(project)
        where = " AND ".join(clauses)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT
                    job_id,
                    event_id,
                    kind,
                    hook_type,
                    project,
                    status,
                    attempts,
                    created_at,
                    updated_at,
                    completed_at,
                    LENGTH(CAST(payload_json AS BLOB)) AS payload_bytes,
                    LENGTH(CAST(COALESCE(result_json, '') AS BLOB)) AS result_bytes
                FROM hook_jobs
                WHERE {where}
                ORDER BY sequence ASC
                """,
                params,
            ).fetchall()
        return [
            {
                "jobId": str(row["job_id"]),
                "eventId": str(row["event_id"]),
                "kind": str(row["kind"]),
                "hookType": str(row["hook_type"]),
                "project": str(row["project"]),
                "status": str(row["status"]),
                "attempts": int(row["attempts"]),
                "createdAt": str(row["created_at"]),
                "updatedAt": str(row["updated_at"]),
                "completedAt": str(row["completed_at"] or row["updated_at"]),
                "payloadBytes": int(row["payload_bytes"] or 0),
                "resultBytes": int(row["result_bytes"] or 0),
            }
            for row in rows
        ]

    def expire_terminal(
        self,
        job_ids: Sequence[str],
        *,
        reason: str,
        policy_name: str,
        purged_at: str | None = None,
    ) -> HookExpireResult:
        normalized_ids = list(dict.fromkeys(str(job_id).strip() for job_id in job_ids if str(job_id).strip()))
        if not normalized_ids:
            return HookExpireResult(
                expired=0,
                payload_bytes=0,
                result_bytes=0,
                job_ids=(),
                checkpoint="not-needed",
            )
        self.initialize()
        tombstoned_at = purged_at or _utc_now_iso()

        def write() -> tuple[int, int, int, tuple[str, ...]]:
            connection = self._connect()
            try:
                connection.execute("PRAGMA secure_delete=ON")
                connection.execute("BEGIN IMMEDIATE")
                expired_ids: list[str] = []
                payload_bytes = 0
                result_bytes = 0
                for job_id in normalized_ids:
                    row = connection.execute(
                        """
                        SELECT *
                        FROM hook_jobs
                        WHERE job_id = ? AND status IN ('completed', 'failed')
                        """,
                        (job_id,),
                    ).fetchone()
                    if row is None:
                        continue
                    payload_size = len(str(row["payload_json"]).encode("utf-8"))
                    result_size = len(str(row["result_json"] or "").encode("utf-8"))
                    connection.execute(
                        """
                        INSERT INTO hook_job_tombstones(
                            job_id,
                            event_id,
                            kind,
                            hook_type,
                            project,
                            payload_sha256,
                            final_status,
                            attempts,
                            created_at,
                            completed_at,
                            payload_bytes,
                            result_bytes,
                            purged_at,
                            purge_reason,
                            policy_name
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row["job_id"]),
                            str(row["event_id"]),
                            str(row["kind"]),
                            str(row["hook_type"]),
                            str(row["project"]),
                            str(row["payload_sha256"]),
                            str(row["status"]),
                            int(row["attempts"]),
                            str(row["created_at"]),
                            str(row["completed_at"] or row["updated_at"]),
                            payload_size,
                            result_size,
                            tombstoned_at,
                            str(reason)[:1000] or None,
                            str(policy_name)[:200] or None,
                        ),
                    )
                    connection.execute("DELETE FROM hook_jobs WHERE job_id = ?", (job_id,))
                    expired_ids.append(job_id)
                    payload_bytes += payload_size
                    result_bytes += result_size
                connection.commit()
                return len(expired_ids), payload_bytes, result_bytes, tuple(expired_ids)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        expired, payload_bytes, result_bytes, expired_ids = self._with_write_retry(write)
        checkpoint = self._checkpoint_wal()
        return HookExpireResult(
            expired=expired,
            payload_bytes=payload_bytes,
            result_bytes=result_bytes,
            job_ids=expired_ids,
            checkpoint=checkpoint,
        )

    def get(self, job_id: str, *, include_payload: bool = False) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM hook_jobs WHERE job_id = ?", (job_id,)).fetchone()
            tombstone = None
            if row is None:
                tombstone = connection.execute(
                    "SELECT * FROM hook_job_tombstones WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
        if row is not None:
            return self._materialize_job(row, include_payload=include_payload)
        if tombstone is not None:
            return self._materialize_tombstone(tombstone)
        return None

    def status(self, *, integrity_check: bool = False) -> dict[str, Any]:
        self.initialize()
        now_epoch = time.time()
        with closing(self._connect()) as connection:
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            rows = connection.execute("SELECT status, COUNT(*) AS count FROM hook_jobs GROUP BY status").fetchall()
            oldest = connection.execute(
                "SELECT MIN(available_at) AS oldest_available_at FROM hook_jobs WHERE status = 'queued'"
            ).fetchone()
            tombstone_rows = connection.execute(
                "SELECT final_status, COUNT(*) AS count FROM hook_job_tombstones GROUP BY final_status"
            ).fetchall()
            tombstone_bytes = connection.execute(
                """
                SELECT
                    COALESCE(SUM(payload_bytes), 0) AS payload_bytes,
                    COALESCE(SUM(result_bytes), 0) AS result_bytes
                FROM hook_job_tombstones
                """
            ).fetchone()
            integrity = "not-run"
            if integrity_check:
                integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])

        counts = {"queued": 0, "processing": 0, "completed": 0, "failed": 0}
        for row in rows:
            counts[str(row["status"])] = int(row["count"])
        tombstone_counts = {"completed": 0, "failed": 0}
        for row in tombstone_rows:
            tombstone_counts[str(row["final_status"])] = int(row["count"])
        pending = counts["queued"] + counts["processing"]
        oldest_available_at = oldest["oldest_available_at"]
        oldest_age_ms = 0
        if oldest_available_at is not None:
            oldest_age_ms = max(int((now_epoch - float(oldest_available_at)) * 1000), 0)
        return {
            "path": str(self.path),
            "schemaVersion": user_version,
            "journalMode": journal_mode,
            "busyTimeoutMs": HOOK_QUEUE_BUSY_TIMEOUT_MS,
            "capacity": self.capacity,
            "pending": pending,
            "available": max(self.capacity - pending, 0),
            "counts": counts,
            "tombstones": tombstone_counts,
            "tombstonesTotal": sum(tombstone_counts.values()),
            "tombstonedPayloadBytes": int(tombstone_bytes["payload_bytes"]),
            "tombstonedResultBytes": int(tombstone_bytes["result_bytes"]),
            "oldestQueuedAgeMs": oldest_age_ms,
            "integrity": integrity,
            "databaseBytes": self.path.stat().st_size if self.path.exists() else 0,
            "walBytes": self._sidecar_size("-wal"),
            "shmBytes": self._sidecar_size("-shm"),
        }

    def backup_to(self, target: Path | str, *, overwrite: bool = False) -> Path:
        self.initialize()
        target_path = assert_safe_path(target)
        if target_path.exists() and not overwrite:
            raise FileExistsError(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        assert_safe_path(target_path.parent)
        if overwrite:
            for candidate in (
                target_path,
                Path(f"{target_path}-wal"),
                Path(f"{target_path}-shm"),
            ):
                assert_safe_path(candidate)
                if candidate.exists():
                    candidate.unlink()
        source_connection = self._connect()
        target_connection = sqlite3.connect(str(target_path))
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        return target_path

    def _pending_count_connection(self, connection: sqlite3.Connection) -> int:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM hook_jobs WHERE status IN ('queued', 'processing')"
            ).fetchone()[0]
        )

    @staticmethod
    def _materialize_job(row: sqlite3.Row, *, include_payload: bool) -> dict[str, Any]:
        job = {
            "sequence": int(row["sequence"]),
            "jobId": str(row["job_id"]),
            "eventId": str(row["event_id"]),
            "kind": str(row["kind"]),
            "hookType": str(row["hook_type"]),
            "sessionId": str(row["session_id"]),
            "project": str(row["project"]),
            "priority": int(row["priority"]),
            "status": str(row["status"]),
            "attempts": int(row["attempts"]),
            "maxAttempts": int(row["max_attempts"]),
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
            "startedAt": str(row["started_at"] or ""),
            "completedAt": str(row["completed_at"] or ""),
            "lastError": str(row["last_error"] or ""),
        }
        if row["result_json"]:
            job["result"] = json.loads(str(row["result_json"]))
        if include_payload:
            job["payload"] = json.loads(str(row["payload_json"]))
        return job

    @staticmethod
    def _materialize_tombstone(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "jobId": str(row["job_id"]),
            "eventId": str(row["event_id"]),
            "kind": str(row["kind"]),
            "hookType": str(row["hook_type"]),
            "project": str(row["project"]),
            "status": str(row["final_status"]),
            "attempts": int(row["attempts"]),
            "createdAt": str(row["created_at"]),
            "completedAt": str(row["completed_at"]),
            "purgedAt": str(row["purged_at"]),
            "retentionPolicy": str(row["policy_name"] or ""),
            "tombstoned": True,
        }

    def _with_write_retry(self, operation, *, priority: bool = False):
        if priority:
            with self._priority_lock:
                self._enqueue_waiters += 1
        try:
            if not priority:
                priority_deadline = time.monotonic() + HOOK_QUEUE_ENQUEUE_PRIORITY_HEAD_START_SECONDS
                while time.monotonic() < priority_deadline:
                    with self._priority_lock:
                        if self._enqueue_waiters == 0:
                            break
                    time.sleep(0.001)
            with self._write_lock:
                delays = (0.0, *HOOK_QUEUE_WRITE_RETRY_DELAYS)
                for index, delay in enumerate(delays):
                    if delay:
                        time.sleep(delay)
                    try:
                        return operation()
                    except sqlite3.OperationalError as exc:
                        locked = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
                        if not locked or index == len(delays) - 1:
                            raise
        finally:
            if priority:
                with self._priority_lock:
                    self._enqueue_waiters -= 1
        raise HookQueueError("unreachable SQLite hook queue retry state")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=HOOK_QUEUE_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={HOOK_QUEUE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA wal_autocheckpoint=1000")
        return connection

    def _sidecar_size(self, suffix: str) -> int:
        sidecar = Path(f"{self.path}{suffix}")
        try:
            return sidecar.stat().st_size
        except FileNotFoundError:
            # SQLite may remove a WAL/SHM sidecar between an existence check
            # and stat while another connection checkpoints it.
            return 0

    def _checkpoint_wal(self) -> str:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            return ":".join(str(value) for value in row) if row is not None else "ok"
        except sqlite3.OperationalError as exc:
            return f"deferred:{exc}"
