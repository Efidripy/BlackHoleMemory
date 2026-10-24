"""Base dependencies, stores, and shared helpers for LLM routes.

Part of llm_routes split (FIX-017). Lower layer containing shared base dependencies.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from fastapi import HTTPException

from .app_runtime import _env_int
from .llm_cache import LLMCacheStore, default_llm_cache_path
from .llm_job_queue import LLMJobQueue, default_llm_job_queue_path
from .llm_learning import LLMLearningStore, default_llm_learning_path
from .llm_long_tasks import LongTaskStore, default_long_task_store_path
from .observation_security import redact_secret_text


class _Deps:
    """Late-bound shared callables supplied by app.py."""

    def __getattr__(self, name: str):
        raise RuntimeError(f"{__name__}: dependency {name!r} is not configured")


_deps = _Deps()


def configure(**callables) -> None:
    for key, value in callables.items():
        setattr(_deps, key, value)


_LLM_JOB_QUEUE_CAPACITY = _env_int("BHM_LLM_JOB_QUEUE_CAPACITY", 128, 1)


_LLM_JOB_QUEUE = LLMJobQueue(default_llm_job_queue_path(), capacity=_LLM_JOB_QUEUE_CAPACITY)


_LLM_LONG_TASK_STORE = LongTaskStore(default_long_task_store_path())


_LLM_CACHE_STORE = LLMCacheStore(default_llm_cache_path())


_LLM_LEARNING_STORE = LLMLearningStore(default_llm_learning_path())


def _llm_public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job.get("job_id"),
        "job_type": job.get("job_type"),
        "project": job.get("project"),
        "priority": job.get("priority"),
        "status": job.get("status"),
        "attempts": job.get("attempts"),
        "max_attempts": job.get("max_attempts"),
        "available_at": job.get("available_at"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "last_error": redact_secret_text(str(job.get("last_error") or "")).value[:1000] or None,
        "checkpoint_digest": job.get("checkpoint_digest"),
        "result_available": job.get("result") is not None,
    }


def _repository_intelligence_scope(raw_root: str) -> str:
    """Normalize a request scope without turning it into a filesystem path."""

    raw = str(raw_root or ".").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or len(raw) > 240
        or "\x00" in raw
        or path.is_absolute()
        or re.match(r"^[A-Za-z]:", raw)
        or ".." in path.parts
    ):
        raise HTTPException(status_code=422, detail={"error": "repository_root_outside_allowlist"})
    return "." if path.as_posix() == "." else path.as_posix().strip("/")
