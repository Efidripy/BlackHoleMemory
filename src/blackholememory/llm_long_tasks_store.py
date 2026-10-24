"""SQLite WAL store for long-task plans, content-addressed chunks, stages and cache."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from .context_compiler import estimate_tokens
from .filesystem_boundaries import assert_safe_path
from .llm_long_tasks_base import (
    LLM_LONG_TASK_BUSY_TIMEOUT_MS,
    LLM_LONG_TASK_CACHE_TTL_SECONDS,
    LLM_LONG_TASK_MAX_CACHE_ENTRIES,
    LLM_LONG_TASK_MAX_CACHE_RESULT_BYTES,
    LLM_LONG_TASK_MAX_CHECKPOINT_BYTES,
    LLM_LONG_TASK_MAX_PLAN_BYTES,
    LLM_LONG_TASK_PLAN_VERSION,
    LLM_LONG_TASK_SCHEMA_VERSION,
    LLM_LONG_TASK_WRITE_RETRY_DELAYS,
    LongTaskBoundsError,
    LongTaskCollision,
    LongTaskError,
    LongTaskNotFound,
    LongTaskPlan,
    _canonical_json,
    _counts,
    _sha256,
    _utc_now_iso,
)
from .llm_safety import sanitize_llm_value


class LongTaskStore:
    """SQLite WAL store for plans, content-addressed chunks and resumable stages."""

    def __init__(
        self,
        path: Path | str,
        *,
        max_cache_entries: int = LLM_LONG_TASK_MAX_CACHE_ENTRIES,
        cache_ttl_seconds: float = LLM_LONG_TASK_CACHE_TTL_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.max_cache_entries = max(1, int(max_cache_entries))
        self.cache_ttl_seconds = max(float(cache_ttl_seconds), 1.0)
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

            def create_schema() -> None:
                assert_safe_path(self.path)
                with closing(self._connect()) as connection:
                    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if current_version not in {0, LLM_LONG_TASK_SCHEMA_VERSION}:
                        raise LongTaskError(
                            f"unsupported long-task schema {current_version}; expected {LLM_LONG_TASK_SCHEMA_VERSION}"
                        )
                    journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold()
                    if journal_mode != "wal":
                        raise LongTaskError(f"SQLite refused WAL mode for {self.path}: {journal_mode}")
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS llm_long_tasks (
                            task_id TEXT PRIMARY KEY,
                            task_key TEXT NOT NULL UNIQUE,
                            project TEXT NOT NULL,
                            plan_digest TEXT NOT NULL,
                            plan_json TEXT NOT NULL,
                            status TEXT NOT NULL CHECK (status IN ('planned', 'processing', 'completed', 'failed', 'cancelled')),
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS llm_long_task_chunks (
                            chunk_id TEXT PRIMARY KEY,
                            project TEXT NOT NULL,
                            digest TEXT NOT NULL UNIQUE,
                            text_value TEXT NOT NULL,
                            source_ids_json TEXT NOT NULL,
                            estimated_tokens INTEGER NOT NULL,
                            created_at TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS llm_long_task_members (
                            task_id TEXT NOT NULL,
                            chunk_id TEXT NOT NULL,
                            ordinal INTEGER NOT NULL,
                            PRIMARY KEY (task_id, chunk_id),
                            FOREIGN KEY (task_id) REFERENCES llm_long_tasks(task_id) ON DELETE CASCADE,
                            FOREIGN KEY (chunk_id) REFERENCES llm_long_task_chunks(chunk_id) ON DELETE RESTRICT
                        );

                        CREATE TABLE IF NOT EXISTS llm_long_task_stages (
                            stage_id TEXT PRIMARY KEY,
                            task_id TEXT NOT NULL,
                            kind TEXT NOT NULL CHECK (kind IN ('map', 'reduce', 'final_reduce')),
                            level INTEGER NOT NULL,
                            ordinal INTEGER NOT NULL,
                            input_ids_json TEXT NOT NULL,
                            dependencies_json TEXT NOT NULL,
                            pack_json TEXT NOT NULL,
                            status TEXT NOT NULL CHECK (status IN ('queued', 'processing', 'completed', 'failed', 'cancelled')),
                            result_json TEXT,
                            result_sha256 TEXT,
                            checkpoint_json TEXT,
                            checkpoint_digest TEXT,
                            last_error TEXT,
                            updated_at TEXT NOT NULL,
                            FOREIGN KEY (task_id) REFERENCES llm_long_tasks(task_id) ON DELETE CASCADE
                        );

                        CREATE INDEX IF NOT EXISTS idx_llm_long_task_stages_ready
                            ON llm_long_task_stages(task_id, status, level, ordinal);

                        CREATE TABLE IF NOT EXISTS llm_long_task_cache (
                            cache_key TEXT PRIMARY KEY,
                            content_digest TEXT NOT NULL,
                            prompt_version TEXT NOT NULL,
                            model_digest TEXT NOT NULL,
                            parameters_json TEXT NOT NULL,
                            result_json TEXT NOT NULL,
                            result_sha256 TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            last_accessed_at TEXT NOT NULL,
                            expires_at REAL NOT NULL,
                            size_bytes INTEGER NOT NULL
                        );
                        """
                    )
                    connection.execute(f"PRAGMA user_version={LLM_LONG_TASK_SCHEMA_VERSION}")
                    connection.execute("PRAGMA wal_autocheckpoint=1000")

            self._with_write_retry(create_schema)
            self._initialized = True

    def create_plan(self, plan: LongTaskPlan) -> dict[str, Any]:
        payload = plan.as_dict(include_chunk_text=False)
        plan_json = _canonical_json(
            {
                key: value
                for key, value in payload.items()
                if key not in {"execution_enabled", "authority", "auto_apply"}
            }
        )
        if len(plan_json.encode("utf-8")) > LLM_LONG_TASK_MAX_PLAN_BYTES:
            raise LongTaskBoundsError("long-task plan exceeds durable plan limit")
        self.initialize()
        now = _utc_now_iso()

        def write() -> dict[str, Any]:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT task_id, plan_digest, status, created_at FROM llm_long_tasks WHERE task_id = ?",
                    (plan.task_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["plan_digest"]) != plan.plan_digest:
                        raise LongTaskCollision(plan.task_id)
                    connection.commit()
                    return {
                        "task_id": plan.task_id,
                        "plan_digest": plan.plan_digest,
                        "status": str(existing["status"]),
                        "inserted": False,
                        "created_at": str(existing["created_at"]),
                    }
                connection.execute(
                    """
                    INSERT INTO llm_long_tasks(task_id, task_key, project, plan_digest, plan_json, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'planned', ?, ?)
                    """,
                    (plan.task_id, plan.task_key, plan.project, plan.plan_digest, plan_json, now, now),
                )
                for chunk in plan.chunks:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO llm_long_task_chunks(
                            chunk_id, project, digest, text_value, source_ids_json, estimated_tokens, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk["chunk_id"],
                            plan.project,
                            chunk["digest"],
                            chunk["text"],
                            _canonical_json(chunk.get("source_ids", [])),
                            int(chunk.get("estimated_tokens", estimate_tokens(chunk["text"]))),
                            now,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO llm_long_task_members(task_id, chunk_id, ordinal) VALUES (?, ?, ?)",
                        (plan.task_id, chunk["chunk_id"], int(chunk["ordinal"])),
                    )
                for stage in plan.stages:
                    connection.execute(
                        """
                        INSERT INTO llm_long_task_stages(
                            stage_id, task_id, kind, level, ordinal, input_ids_json, dependencies_json,
                            pack_json, status, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                        """,
                        (
                            stage["stage_id"],
                            plan.task_id,
                            stage["kind"],
                            int(stage["level"]),
                            int(stage["ordinal"]),
                            _canonical_json(stage["input_ids"]),
                            _canonical_json(stage["dependencies"]),
                            _canonical_json(stage["pack"]),
                            now,
                        ),
                    )
                connection.commit()
                return {
                    "task_id": plan.task_id,
                    "plan_digest": plan.plan_digest,
                    "status": "planned",
                    "inserted": True,
                    "created_at": now,
                }
            except Exception:  # broad: rollback transaction before re-raising to preserve task failure semantics
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write, priority=True)

    def get_plan(self, task_id: str, *, include_chunks: bool = False) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        self.initialize()
        with closing(self._connect()) as connection:
            task = connection.execute("SELECT * FROM llm_long_tasks WHERE task_id = ?", (str(task_id),)).fetchone()
            if task is None:
                return None
            stages = connection.execute(
                "SELECT * FROM llm_long_task_stages WHERE task_id = ? ORDER BY level, ordinal",
                (str(task_id),),
            ).fetchall()
            chunks = connection.execute(
                """
                SELECT c.*, m.ordinal
                FROM llm_long_task_members m
                JOIN llm_long_task_chunks c ON c.chunk_id = m.chunk_id
                WHERE m.task_id = ?
                ORDER BY m.ordinal
                """,
                (str(task_id),),
            ).fetchall()
        return {
            "schema_version": LLM_LONG_TASK_PLAN_VERSION,
            "task_id": str(task["task_id"]),
            "task_key": str(task["task_key"]),
            "project": str(task["project"]),
            "plan_digest": str(task["plan_digest"]),
            "status": str(task["status"]),
            "created_at": str(task["created_at"]),
            "updated_at": str(task["updated_at"]),
            "chunks": [self._materialize_chunk(row, include_text=include_chunks) for row in chunks],
            "stages": [self._materialize_stage(row) for row in stages],
            "stage_counts": _counts(str(row["status"]) for row in stages),
            "execution_enabled": False,
            "authority": "proposal",
            "auto_apply": False,
        }

    def ready_stages(self, task_id: str, *, limit: int = 16) -> list[dict[str, Any]]:
        plan = self.get_plan(task_id)
        if plan is None:
            raise LongTaskNotFound(str(task_id))
        stage_map = {str(stage["stage_id"]): stage for stage in plan["stages"]}
        ready: list[dict[str, Any]] = []
        for stage in plan["stages"]:
            if stage["status"] != "queued":
                continue
            dependencies = [str(item) for item in stage.get("dependencies", [])]
            if all(stage_map.get(item, {}).get("status") == "completed" for item in dependencies):
                ready.append(stage)
            if len(ready) >= max(min(int(limit), 128), 1):
                break
        return ready

    def checkpoint(
        self,
        task_id: str,
        stage_id: str,
        *,
        status: str,
        result: Any = None,
        checkpoint: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        normalized_status = str(status or "").strip().casefold()
        if normalized_status not in {"processing", "completed", "failed", "cancelled"}:
            raise LongTaskError(f"unsupported long-task checkpoint status: {status}")
        safe_result = None
        result_json = None
        result_digest = None
        if result is not None:
            safe_result = sanitize_llm_value(result, source="llm-long-task-result").value
            result_json = _canonical_json(safe_result)
            if len(result_json.encode("utf-8")) > LLM_LONG_TASK_MAX_CACHE_RESULT_BYTES:
                raise LongTaskBoundsError("long-task stage result exceeds durable result limit")
            result_digest = _sha256(result_json)
        checkpoint_json = None
        checkpoint_digest = None
        if checkpoint is not None:
            checkpoint_json = _canonical_json(dict(checkpoint))
            if len(checkpoint_json.encode("utf-8")) > LLM_LONG_TASK_MAX_CHECKPOINT_BYTES:
                raise LongTaskBoundsError("long-task checkpoint exceeds durable checkpoint limit")
            checkpoint_digest = _sha256(checkpoint_json)
        self.initialize()

        def write() -> dict[str, Any]:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT task_id FROM llm_long_task_stages WHERE task_id = ? AND stage_id = ?",
                    (str(task_id), str(stage_id)),
                ).fetchone()
                if row is None:
                    raise LongTaskNotFound(f"{task_id}/{stage_id}")
                now = _utc_now_iso()
                connection.execute(
                    """
                    UPDATE llm_long_task_stages
                    SET status = ?, result_json = COALESCE(?, result_json), result_sha256 = COALESCE(?, result_sha256),
                        checkpoint_json = COALESCE(?, checkpoint_json), checkpoint_digest = COALESCE(?, checkpoint_digest),
                        last_error = ?, updated_at = ?
                    WHERE task_id = ? AND stage_id = ?
                    """,
                    (
                        normalized_status,
                        result_json,
                        result_digest,
                        checkpoint_json,
                        checkpoint_digest,
                        str(error or "")[:4_000] or None,
                        now,
                        str(task_id),
                        str(stage_id),
                    ),
                )
                counts = {
                    str(item["status"]): int(item["count"])
                    for item in connection.execute(
                        "SELECT status, COUNT(*) AS count FROM llm_long_task_stages WHERE task_id = ? GROUP BY status",
                        (str(task_id),),
                    ).fetchall()
                }
                if counts.get("completed", 0) == sum(counts.values()):
                    task_status = "completed"
                elif counts.get("failed", 0) or counts.get("cancelled", 0):
                    task_status = "failed"
                elif counts.get("processing", 0):
                    task_status = "processing"
                else:
                    task_status = "planned"
                connection.execute(
                    "UPDATE llm_long_tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                    (task_status, now, str(task_id)),
                )
                connection.commit()
                return {"task_id": str(task_id), "stage_id": str(stage_id), "status": normalized_status, "task_status": task_status, "result_sha256": result_digest, "checkpoint_digest": checkpoint_digest}
            except Exception:  # broad: rollback transaction before re-raising to preserve task failure semantics
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write)

    def resume(self, task_id: str, *, limit: int = 16) -> dict[str, Any]:
        plan = self.get_plan(task_id)
        if plan is None:
            raise LongTaskNotFound(str(task_id))
        return {**plan, "ready_stages": self.ready_stages(task_id, limit=limit), "resumed": True}

    def cache_get(self, cache_key: str) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        self.initialize()
        now = time.time()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM llm_long_task_cache WHERE cache_key = ?",
                (str(cache_key),),
            ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"]) <= now:
                connection.execute("DELETE FROM llm_long_task_cache WHERE cache_key = ?", (str(cache_key),))
                return None
            connection.execute(
                "UPDATE llm_long_task_cache SET last_accessed_at = ? WHERE cache_key = ?",
                (_utc_now_iso(), str(cache_key)),
            )
        return {
            "cache_key": str(row["cache_key"]),
            "content_digest": str(row["content_digest"]),
            "prompt_version": str(row["prompt_version"]),
            "model_digest": str(row["model_digest"]),
            "parameters": json.loads(str(row["parameters_json"])),
            "result": json.loads(str(row["result_json"])),
            "result_sha256": str(row["result_sha256"]),
            "created_at": str(row["created_at"]),
            "expires_at": float(row["expires_at"]),
        }

    def cache_put(
        self,
        cache_key: str,
        *,
        content_digest: str,
        prompt_version: str,
        model_digest: str,
        parameters: Mapping[str, Any] | None,
        result: Any,
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        safe_result = sanitize_llm_value(result, source="llm-long-task-cache").value
        result_json = _canonical_json(safe_result)
        size_bytes = len(result_json.encode("utf-8"))
        if size_bytes > LLM_LONG_TASK_MAX_CACHE_RESULT_BYTES:
            raise LongTaskBoundsError("long-task cache result exceeds durable result limit")
        normalized_key = str(cache_key or "").strip()
        if not normalized_key:
            raise LongTaskError("cache_key is required")
        self.initialize()
        now = time.time()
        created_at = _utc_now_iso()
        expires_at = now + max(float(ttl_seconds if ttl_seconds is not None else self.cache_ttl_seconds), 1.0)
        parameters_json = _canonical_json(dict(parameters or {}))
        result_digest = _sha256(result_json)

        def write() -> dict[str, Any]:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT result_sha256 FROM llm_long_task_cache WHERE cache_key = ?",
                    (normalized_key,),
                ).fetchone()
                if existing is not None and str(existing["result_sha256"]) != result_digest:
                    raise LongTaskCollision(normalized_key)
                connection.execute(
                    """
                    INSERT INTO llm_long_task_cache(
                        cache_key, content_digest, prompt_version, model_digest, parameters_json,
                        result_json, result_sha256, created_at, last_accessed_at, expires_at, size_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        last_accessed_at = excluded.last_accessed_at,
                        expires_at = excluded.expires_at
                    """,
                    (
                        normalized_key,
                        str(content_digest),
                        str(prompt_version),
                        str(model_digest),
                        parameters_json,
                        result_json,
                        result_digest,
                        created_at,
                        created_at,
                        expires_at,
                        size_bytes,
                    ),
                )
                connection.execute("DELETE FROM llm_long_task_cache WHERE expires_at <= ?", (now,))
                overflow = connection.execute(
                    """
                    SELECT cache_key FROM llm_long_task_cache
                    ORDER BY last_accessed_at DESC, created_at DESC
                    LIMIT -1 OFFSET ?
                    """,
                    (self.max_cache_entries,),
                ).fetchall()
                if overflow:
                    connection.executemany(
                        "DELETE FROM llm_long_task_cache WHERE cache_key = ?",
                        [(str(row["cache_key"]),) for row in overflow],
                    )
                connection.commit()
                return {
                    "cache_key": normalized_key,
                    "result_sha256": result_digest,
                    "size_bytes": size_bytes,
                    "expires_at": expires_at,
                }
            except Exception:  # broad: rollback transaction before re-raising to preserve task failure semantics
                connection.rollback()
                raise
            finally:
                connection.close()

        return self._with_write_retry(write, priority=True)

    def status(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema_version": LLM_LONG_TASK_SCHEMA_VERSION,
                "path": str(self.path),
                "exists": False,
                "tasks": 0,
                "chunks": 0,
                "stages": 0,
                "cache_entries": 0,
            }
        self.initialize()
        with closing(self._connect()) as connection:
            counts = {
                "tasks": int(connection.execute("SELECT COUNT(*) FROM llm_long_tasks").fetchone()[0]),
                "chunks": int(connection.execute("SELECT COUNT(*) FROM llm_long_task_chunks").fetchone()[0]),
                "stages": int(connection.execute("SELECT COUNT(*) FROM llm_long_task_stages").fetchone()[0]),
                "cache_entries": int(connection.execute("SELECT COUNT(*) FROM llm_long_task_cache").fetchone()[0]),
            }
        return {"schema_version": LLM_LONG_TASK_SCHEMA_VERSION, "path": str(self.path), "exists": True, **counts}

    @staticmethod
    def _materialize_chunk(row: sqlite3.Row, *, include_text: bool) -> dict[str, Any]:
        result = {
            "chunk_id": str(row["chunk_id"]),
            "project": str(row["project"]),
            "digest": str(row["digest"]),
            "source_ids": json.loads(str(row["source_ids_json"])),
            "estimated_tokens": int(row["estimated_tokens"]),
            "ordinal": int(row["ordinal"]),
        }
        if include_text:
            result["text"] = str(row["text_value"])
        return result

    @staticmethod
    def _materialize_stage(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "stage_id": str(row["stage_id"]),
            "task_id": str(row["task_id"]),
            "kind": str(row["kind"]),
            "level": int(row["level"]),
            "ordinal": int(row["ordinal"]),
            "input_ids": json.loads(str(row["input_ids_json"])),
            "dependencies": json.loads(str(row["dependencies_json"])),
            "pack": json.loads(str(row["pack_json"])),
            "status": str(row["status"]),
            "result": json.loads(str(row["result_json"])) if row["result_json"] else None,
            "result_sha256": str(row["result_sha256"]) if row["result_sha256"] else None,
            "checkpoint": json.loads(str(row["checkpoint_json"])) if row["checkpoint_json"] else None,
            "checkpoint_digest": str(row["checkpoint_digest"]) if row["checkpoint_digest"] else None,
            "last_error": str(row["last_error"]) if row["last_error"] else None,
            "updated_at": str(row["updated_at"]),
        }

    def _connect(self) -> sqlite3.Connection:
        assert_safe_path(self.path)
        connection = sqlite3.connect(
            self.path,
            timeout=LLM_LONG_TASK_BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={LLM_LONG_TASK_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _with_write_retry(self, operation, *, priority: bool = False):
        delays = (0.0,) + LLM_LONG_TASK_WRITE_RETRY_DELAYS
        if priority:
            delays = (0.0, 0.0) + LLM_LONG_TASK_WRITE_RETRY_DELAYS
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
        raise LongTaskError("SQLite remained locked during long-task write") from last_error


def default_long_task_store_path() -> Path:
    import os

    configured = str(os.getenv("BHM_LLM_LONG_TASK_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / ".runtime" / "llm-jobs" / "long-tasks.sqlite3"


__all__ = [
    "LongTaskStore",
    "default_long_task_store_path",
]
