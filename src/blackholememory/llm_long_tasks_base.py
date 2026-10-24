"""Base types, constants, errors and deterministic IDs for LLM long tasks."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

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
    "LLM_LONG_TASK_BUSY_TIMEOUT_MS",
    "LLM_LONG_TASK_CACHE_TTL_SECONDS",
    "LLM_LONG_TASK_CACHE_VERSION",
    "LLM_LONG_TASK_MAX_CACHE_ENTRIES",
    "LLM_LONG_TASK_MAX_CACHE_RESULT_BYTES",
    "LLM_LONG_TASK_MAX_CHECKPOINT_BYTES",
    "LLM_LONG_TASK_MAX_CHUNKS",
    "LLM_LONG_TASK_MAX_CHUNK_CHARS",
    "LLM_LONG_TASK_MAX_FANOUT",
    "LLM_LONG_TASK_MAX_INPUT_BYTES",
    "LLM_LONG_TASK_MAX_ITEMS",
    "LLM_LONG_TASK_MAX_LEVELS",
    "LLM_LONG_TASK_MAX_PLAN_BYTES",
    "LLM_LONG_TASK_MIN_CHUNK_CHARS",
    "LLM_LONG_TASK_PLAN_VERSION",
    "LLM_LONG_TASK_SCHEMA_VERSION",
    "LLM_LONG_TASK_WRITE_RETRY_DELAYS",
    "LongTaskBoundsError",
    "LongTaskCollision",
    "LongTaskError",
    "LongTaskNotFound",
    "LongTaskPlan",
    "_canonical_json",
    "_counts",
    "_groups",
    "_sha256",
    "_utc_now_iso",
    "content_addressed_chunk_id",
    "deterministic_long_task_cache_key",
    "deterministic_long_task_id",
]
