"""Durable, bounded map/shard/reduce planning for local-LLM long tasks.

P17.8 deliberately stops at a deterministic control plane.  It persists
sanitized content-addressed chunks, stage dependencies, checkpoints and a
bounded result cache, but it never calls a model and never applies a result.
Workers in later plan items can claim ready stages and resume from the same
SQLite WAL state after a restart.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .context_compiler import compile_context
from .context_compiler import estimate_tokens
from .filesystem_boundaries import assert_safe_path
from .llm_safety import sanitize_llm_value


LLM_LONG_TASK_SCHEMA_VERSION = 1
LLM_LONG_TASK_PLAN_VERSION = "bhm.llm.long-task.v1"
LLM_LONG_TASK_CACHE_VERSION = "bhm.llm.long-task-cache.v1"
LLM_LONG_TASK_BUSY_TIMEOUT_MS = 5_000
LLM_LONG_TASK_WRITE_RETRY_DELAYS = (0.025, 0.05, 0.1, 0.2, 0.4)
LLM_LONG_TASK_MAX_INPUT_BYTES = 512 * 1024
LLM_LONG_TASK_MAX_PLAN_BYTES = 256 * 1024
LLM_LONG_TASK_MAX_CHUNKS = 128
LLM_LONG_TASK_MAX_CHUNK_CHARS = 16_000
LLM_LONG_TASK_MIN_CHUNK_CHARS = 256
LLM_LONG_TASK_MAX_ITEMS = 256
LLM_LONG_TASK_MAX_FANOUT = 8
LLM_LONG_TASK_MAX_LEVELS = 8
LLM_LONG_TASK_MAX_CACHE_ENTRIES = 256
LLM_LONG_TASK_MAX_CACHE_RESULT_BYTES = 64 * 1024
LLM_LONG_TASK_CACHE_TTL_SECONDS = 24 * 60 * 60
LLM_LONG_TASK_MAX_CHECKPOINT_BYTES = 32 * 1024


class LongTaskError(RuntimeError):
    """Base error for long-task planning and persistence."""


class LongTaskBoundsError(LongTaskError):
    """Input or plan exceeds an explicit P17.8 bound."""


class LongTaskCollision(LongTaskError):
    """A deterministic task or cache key already names different content."""

    def __init__(self, identifier: str) -> None:
        self.identifier = str(identifier)
        super().__init__(f"long-task idempotency collision: {self.identifier}")


class LongTaskNotFound(LongTaskError):
    pass


@dataclass(frozen=True)
class LongTaskPlan:
    task_id: str
    task_key: str
    project: str
    plan_digest: str
    chunks: tuple[dict[str, Any], ...]
    stages: tuple[dict[str, Any], ...]
    context_budget_tokens: int
    fanout: int

    def as_dict(self, *, include_chunk_text: bool = False) -> dict[str, Any]:
        chunks = []
        for chunk in self.chunks:
            item = dict(chunk)
            if not include_chunk_text:
                item.pop("text", None)
            chunks.append(item)
        return {
            "schema_version": LLM_LONG_TASK_PLAN_VERSION,
            "task_id": self.task_id,
            "task_key": self.task_key,
            "project": self.project,
            "plan_digest": self.plan_digest,
            "chunk_count": len(self.chunks),
            "stage_count": len(self.stages),
            "context_budget_tokens": self.context_budget_tokens,
            "fanout": self.fanout,
            "chunks": chunks,
            "stages": [dict(stage) for stage in self.stages],
            "execution_enabled": False,
            "authority": "proposal",
            "auto_apply": False,
        }


def deterministic_long_task_id(task_key: str) -> str:
    normalized = str(task_key or "").strip()
    if not normalized:
        raise LongTaskError("task_key is required")
    return f"llmtask_{_sha256(normalized)[:32]}"


def content_addressed_chunk_id(
    text: str,
    *,
    project: str = "blackholememory",
    source_ids: Sequence[str] = (),
) -> str:
    payload = {
        "project": str(project or "blackholememory").strip(),
        "source_ids": sorted({str(value).strip() for value in source_ids if str(value).strip()}),
        "text": str(text or ""),
    }
    return f"chunk_{_sha256(_canonical_json(payload))[:32]}"


def deterministic_long_task_cache_key(
    *,
    content_digest: str,
    prompt_version: str,
    model_digest: str,
    parameters: Mapping[str, Any] | None = None,
) -> str:
    payload = {
        "schema_version": LLM_LONG_TASK_CACHE_VERSION,
        "content_digest": str(content_digest or "").strip(),
        "prompt_version": str(prompt_version or "").strip(),
        "model_digest": str(model_digest or "").strip(),
        "parameters": dict(parameters or {}),
    }
    if not payload["content_digest"] or not payload["prompt_version"] or not payload["model_digest"]:
        raise LongTaskError("content_digest, prompt_version and model_digest are required for cache keys")
    return f"cache_{_sha256(_canonical_json(payload))[:48]}"


def build_long_task_plan(
    task_key: str,
    items: Sequence[Mapping[str, Any]],
    *,
    project: str = "blackholememory",
    chunk_chars: int = 8_000,
    context_budget_tokens: int = 1_200,
    fanout: int = 4,
) -> LongTaskPlan:
    """Build a deterministic map/reduce DAG from sanitized bounded items."""

    normalized_project = str(project or "blackholememory").strip() or "blackholememory"
    normalized_task_key = str(task_key or "").strip()
    task_id = deterministic_long_task_id(normalized_task_key)
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        raise LongTaskError("long-task items must be a sequence of objects")
    if len(items) > LLM_LONG_TASK_MAX_ITEMS:
        raise LongTaskBoundsError(f"long-task item count exceeds {LLM_LONG_TASK_MAX_ITEMS}")
    chunk_limit = max(min(int(chunk_chars), LLM_LONG_TASK_MAX_CHUNK_CHARS), LLM_LONG_TASK_MIN_CHUNK_CHARS)
    budget = max(min(int(context_budget_tokens), 8_000), 64)
    group_size = max(min(int(fanout), LLM_LONG_TASK_MAX_FANOUT), 2)

    safe_items = sanitize_llm_value(
        [dict(item) for item in items],
        source="llm-long-task-plan",
        project=normalized_project,
        max_input_bytes=LLM_LONG_TASK_MAX_INPUT_BYTES,
    ).value
    if not isinstance(safe_items, list):
        raise LongTaskError("sanitized long-task items must remain a list")
    chunks = _build_chunks(safe_items, project=normalized_project, chunk_chars=chunk_limit)
    if not chunks:
        raise LongTaskError("long-task plan requires at least one non-empty content item")
    if len(chunks) > LLM_LONG_TASK_MAX_CHUNKS:
        raise LongTaskBoundsError(f"long-task chunk count exceeds {LLM_LONG_TASK_MAX_CHUNKS}")

    stages: list[dict[str, Any]] = []
    current_ids: list[str] = []
    for ordinal, chunk in enumerate(chunks):
        stage_id = _stage_id(task_id, "map", 0, ordinal, [chunk["chunk_id"]])
        pack = _context_pack(
            [{"id": chunk["chunk_id"], "title": f"Chunk {ordinal + 1}", "content": chunk["text"], "project": normalized_project}],
            token_budget=budget,
            max_item_chars=chunk_limit,
        )
        stages.append(
            _stage_record(
                stage_id=stage_id,
                task_id=task_id,
                kind="map",
                level=0,
                ordinal=ordinal,
                input_ids=[chunk["chunk_id"]],
                dependencies=[],
                pack=pack,
            )
        )
        current_ids.append(stage_id)

    level = 1
    while len(current_ids) > 1:
        if level >= LLM_LONG_TASK_MAX_LEVELS:
            raise LongTaskBoundsError(f"long-task hierarchy exceeds {LLM_LONG_TASK_MAX_LEVELS} levels")
        next_ids: list[str] = []
        for ordinal, group in enumerate(_groups(current_ids, group_size)):
            stage_id = _stage_id(task_id, "reduce", level, ordinal, group)
            placeholders = [
                {
                    "id": input_id,
                    "title": f"Summary input {index + 1}",
                    "content": f"Pending summary reference: {input_id}",
                    "project": normalized_project,
                }
                for index, input_id in enumerate(group)
            ]
            pack = _context_pack(placeholders, token_budget=budget, max_item_chars=chunk_limit)
            stages.append(
                _stage_record(
                    stage_id=stage_id,
                    task_id=task_id,
                    kind="reduce" if len(group) > 1 else "final_reduce",
                    level=level,
                    ordinal=ordinal,
                    input_ids=group,
                    dependencies=group,
                    pack=pack,
                )
            )
            next_ids.append(stage_id)
        current_ids = next_ids
        level += 1
    if current_ids:
        terminal_id = current_ids[0]
        for stage in stages:
            if stage["stage_id"] == terminal_id:
                stage["kind"] = "final_reduce"
                break
    if stages and stages[-1]["kind"] == "map":
        # A one-chunk task still gets a reduce checkpoint so every task has the
        # same resumable terminal contract.
        input_id = stages[-1]["stage_id"]
        stage_id = _stage_id(task_id, "reduce", 1, 0, [input_id])
        pack = _context_pack(
            [{"id": input_id, "title": "Summary input", "content": f"Pending summary reference: {input_id}", "project": normalized_project}],
            token_budget=budget,
            max_item_chars=chunk_limit,
        )
        stages.append(
            _stage_record(
                stage_id=stage_id,
                task_id=task_id,
                kind="final_reduce",
                level=1,
                ordinal=0,
                input_ids=[input_id],
                dependencies=[input_id],
                pack=pack,
            )
        )

    plan_core = {
        "schema_version": LLM_LONG_TASK_PLAN_VERSION,
        "task_id": task_id,
        "task_key": normalized_task_key,
        "project": normalized_project,
        "context_budget_tokens": budget,
        "fanout": group_size,
        "chunks": [{key: value for key, value in chunk.items() if key != "text"} for chunk in chunks],
        "stages": [{key: value for key, value in stage.items() if key not in {"status", "result", "checkpoint"}} for stage in stages],
    }
    plan_json = _canonical_json(plan_core)
    if len(plan_json.encode("utf-8")) > LLM_LONG_TASK_MAX_PLAN_BYTES:
        raise LongTaskBoundsError("long-task plan exceeds durable plan limit")
    plan_digest = _sha256(plan_json)
    return LongTaskPlan(
        task_id=task_id,
        task_key=normalized_task_key,
        project=normalized_project,
        plan_digest=plan_digest,
        chunks=tuple(chunks),
        stages=tuple(stages),
        context_budget_tokens=budget,
        fanout=group_size,
    )


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
            except Exception:
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
            except Exception:
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
            except Exception:
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


def _build_chunks(items: Sequence[Any], *, project: str, chunk_chars: int) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current_blocks: list[str] = []
    current_ids: list[str] = []

    def flush() -> None:
        if not current_blocks:
            return
        text = "\n\n".join(current_blocks).strip()
        if text:
            digest = _sha256(_canonical_json({"project": project, "source_ids": sorted(current_ids), "text": text}))
            chunks.append(
                {
                    "chunk_id": content_addressed_chunk_id(text, project=project, source_ids=current_ids),
                    "digest": digest,
                    "ordinal": len(chunks),
                    "text": text,
                    "source_ids": sorted(set(current_ids)),
                    "estimated_tokens": estimate_tokens(text),
                }
            )
        current_blocks.clear()
        current_ids.clear()

    for index, raw_item in enumerate(items[:LLM_LONG_TASK_MAX_ITEMS]):
        if not isinstance(raw_item, Mapping):
            raise LongTaskError(f"long-task item {index} is not an object")
        source_id = str(raw_item.get("source_id") or raw_item.get("id") or f"item-{index + 1}").strip()
        title = str(raw_item.get("title") or source_id).strip()[:240]
        content = str(raw_item.get("content") or raw_item.get("text") or raw_item.get("memory") or "").strip()
        if not content:
            continue
        for part_index, part in enumerate(_split_text(content, chunk_chars), start=1):
            block_title = title if part_index == 1 and len(_split_text(content, chunk_chars)) == 1 else f"{title} part {part_index}"
            block = f"[{source_id}] {block_title}\n{part}"
            projected = "\n\n".join([*current_blocks, block])
            if current_blocks and len(projected) > chunk_chars:
                flush()
            current_blocks.append(block)
            current_ids.append(source_id)
            if len("\n\n".join(current_blocks)) >= chunk_chars:
                flush()
    flush()
    return chunks


def _split_text(text: str, chunk_chars: int) -> list[str]:
    paragraphs = [item.strip() for item in str(text).splitlines() if item.strip()]
    if not paragraphs:
        return []
    parts: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) <= chunk_chars:
            if current and len(current) + 2 + len(paragraph) > chunk_chars:
                parts.append(current)
                current = ""
            current = f"{current}\n{paragraph}".strip() if current else paragraph
            continue
        if current:
            parts.append(current)
            current = ""
        for offset in range(0, len(paragraph), chunk_chars):
            parts.append(paragraph[offset : offset + chunk_chars].strip())
    if current:
        parts.append(current)
    return [item for item in parts if item]


def _context_pack(items: Sequence[Mapping[str, Any]], *, token_budget: int, max_item_chars: int) -> dict[str, Any]:
    pack = compile_context(items, token_budget=token_budget, max_item_chars=max_item_chars)
    return {
        "text": pack["text"],
        "estimated_tokens": int(pack["estimated_tokens"]),
        "token_budget": int(pack["token_budget"]),
        "truncated": bool(pack["truncated"]),
        "included_count": int(pack["included_count"]),
        "citation_count": int((pack.get("provenance") or {}).get("citation_count", 0)),
        "digest": _sha256(str(pack["text"])),
    }


def _stage_record(
    *,
    stage_id: str,
    task_id: str,
    kind: str,
    level: int,
    ordinal: int,
    input_ids: Sequence[str],
    dependencies: Sequence[str],
    pack: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "task_id": task_id,
        "kind": kind,
        "level": int(level),
        "ordinal": int(ordinal),
        "input_ids": [str(item) for item in input_ids],
        "dependencies": [str(item) for item in dependencies],
        "pack": dict(pack),
        "status": "queued",
        "result": None,
        "checkpoint": None,
    }


def _stage_id(task_id: str, kind: str, level: int, ordinal: int, input_ids: Sequence[str]) -> str:
    return f"stage_{_sha256(_canonical_json({"task_id": task_id, "kind": kind, "level": level, "ordinal": ordinal, "input_ids": list(input_ids)}))[:32]}"


def _groups(values: Sequence[str], size: int) -> list[list[str]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


def _counts(values: Sequence[str] | Any) -> dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str, allow_nan=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "LLM_LONG_TASK_CACHE_VERSION",
    "LLM_LONG_TASK_MAX_CACHE_ENTRIES",
    "LLM_LONG_TASK_MAX_CHUNKS",
    "LLM_LONG_TASK_MAX_CHUNK_CHARS",
    "LLM_LONG_TASK_MAX_FANOUT",
    "LLM_LONG_TASK_PLAN_VERSION",
    "LLM_LONG_TASK_SCHEMA_VERSION",
    "LongTaskBoundsError",
    "LongTaskCollision",
    "LongTaskError",
    "LongTaskNotFound",
    "LongTaskPlan",
    "LongTaskStore",
    "build_long_task_plan",
    "content_addressed_chunk_id",
    "default_long_task_store_path",
    "deterministic_long_task_cache_key",
    "deterministic_long_task_id",
]
