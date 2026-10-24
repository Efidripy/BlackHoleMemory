"""Durable, bounded map/shard/reduce planning for local-LLM long tasks.

P17.8 deliberately stops at a deterministic control plane.  It persists
sanitized content-addressed chunks, stage dependencies, checkpoints and a
bounded result cache, but it never calls a model and never applies a result.
Workers in later plan items can claim ready stages and resume from the same
SQLite WAL state after a restart.
"""

from __future__ import annotations

from .llm_long_tasks_base import (
    LLM_LONG_TASK_BUSY_TIMEOUT_MS as LLM_LONG_TASK_BUSY_TIMEOUT_MS,  # noqa: F401
    LLM_LONG_TASK_CACHE_TTL_SECONDS as LLM_LONG_TASK_CACHE_TTL_SECONDS,  # noqa: F401
    LLM_LONG_TASK_CACHE_VERSION as LLM_LONG_TASK_CACHE_VERSION,  # noqa: F401
    LLM_LONG_TASK_MAX_CACHE_ENTRIES as LLM_LONG_TASK_MAX_CACHE_ENTRIES,  # noqa: F401
    LLM_LONG_TASK_MAX_CACHE_RESULT_BYTES as LLM_LONG_TASK_MAX_CACHE_RESULT_BYTES,  # noqa: F401
    LLM_LONG_TASK_MAX_CHECKPOINT_BYTES as LLM_LONG_TASK_MAX_CHECKPOINT_BYTES,  # noqa: F401
    LLM_LONG_TASK_MAX_CHUNKS as LLM_LONG_TASK_MAX_CHUNKS,  # noqa: F401
    LLM_LONG_TASK_MAX_CHUNK_CHARS as LLM_LONG_TASK_MAX_CHUNK_CHARS,  # noqa: F401
    LLM_LONG_TASK_MAX_FANOUT as LLM_LONG_TASK_MAX_FANOUT,  # noqa: F401
    LLM_LONG_TASK_MAX_INPUT_BYTES as LLM_LONG_TASK_MAX_INPUT_BYTES,  # noqa: F401
    LLM_LONG_TASK_MAX_ITEMS as LLM_LONG_TASK_MAX_ITEMS,  # noqa: F401
    LLM_LONG_TASK_MAX_LEVELS as LLM_LONG_TASK_MAX_LEVELS,  # noqa: F401
    LLM_LONG_TASK_MAX_PLAN_BYTES as LLM_LONG_TASK_MAX_PLAN_BYTES,  # noqa: F401
    LLM_LONG_TASK_MIN_CHUNK_CHARS as LLM_LONG_TASK_MIN_CHUNK_CHARS,  # noqa: F401
    LLM_LONG_TASK_PLAN_VERSION as LLM_LONG_TASK_PLAN_VERSION,  # noqa: F401
    LLM_LONG_TASK_SCHEMA_VERSION as LLM_LONG_TASK_SCHEMA_VERSION,  # noqa: F401
    LLM_LONG_TASK_WRITE_RETRY_DELAYS as LLM_LONG_TASK_WRITE_RETRY_DELAYS,  # noqa: F401
    LongTaskBoundsError as LongTaskBoundsError,  # noqa: F401
    LongTaskCollision as LongTaskCollision,  # noqa: F401
    LongTaskError as LongTaskError,  # noqa: F401
    LongTaskNotFound as LongTaskNotFound,  # noqa: F401
    LongTaskPlan as LongTaskPlan,  # noqa: F401
    _canonical_json as _canonical_json,  # noqa: F401
    _counts as _counts,  # noqa: F401
    _groups as _groups,  # noqa: F401
    _sha256 as _sha256,  # noqa: F401
    _utc_now_iso as _utc_now_iso,  # noqa: F401
    content_addressed_chunk_id as content_addressed_chunk_id,  # noqa: F401
    deterministic_long_task_cache_key as deterministic_long_task_cache_key,  # noqa: F401
    deterministic_long_task_id as deterministic_long_task_id,  # noqa: F401
)
from .llm_long_tasks_plan import (
    _build_chunks as _build_chunks,  # noqa: F401
    _context_pack as _context_pack,  # noqa: F401
    _split_text as _split_text,  # noqa: F401
    _stage_id as _stage_id,  # noqa: F401
    _stage_record as _stage_record,  # noqa: F401
    build_long_task_plan as build_long_task_plan,  # noqa: F401
)
from .llm_long_tasks_store import (
    LongTaskStore as LongTaskStore,  # noqa: F401
    default_long_task_store_path as default_long_task_store_path,  # noqa: F401
)

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
