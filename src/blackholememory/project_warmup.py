"""Warm project provider instances sequentially after the provider probe."""

from __future__ import annotations

import asyncio
import logging
import os

from . import app_core_fallback, app_lifecycle, app_runtime
from .mem0_adapter import get_project_mem0_memory
from .memory_search_multiproject import resolve_retrieval_projects

_LOGGER = logging.getLogger(__name__)
_PAUSE_SECONDS = 0.2


def start_warmup() -> asyncio.Task[None]:
    provider = asyncio.create_task(app_runtime.warmup_provider_probe())
    provider.project_warmup_task = asyncio.create_task(warmup_projects(provider))
    return provider


async def cancel_warmup(provider: asyncio.Task[None] | None) -> None:
    if provider is not None:
        await app_lifecycle._cancel_lifespan_task(provider.project_warmup_task, name="project_warmup")
    await app_lifecycle._cancel_lifespan_task(provider, name="provider_warmup")


async def warmup_projects(provider_warmup: asyncio.Task[None]) -> None:
    if os.getenv("BHM_PROJECT_WARMUP", "1").strip() == "0":
        return
    await asyncio.shield(provider_warmup)
    try:
        projects = await asyncio.to_thread(resolve_retrieval_projects, ["*"], app_runtime._memory_service)
    except Exception:
        _LOGGER.exception("Could not list projects for provider warmup")
        return
    for project in projects:
        while True:
            try:
                worker = asyncio.create_task(
                    app_core_fallback._run_bounded_provider_call(get_project_mem0_memory, project)
                )
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    worker.add_done_callback(app_core_fallback._consume_late_read_worker)
                    raise
                break
            except app_core_fallback.RetrievalProviderCapacityExceeded:
                await asyncio.sleep(_PAUSE_SECONDS)
            except Exception:
                _LOGGER.exception("Project provider warmup failed for %s", project)
                break
        await asyncio.sleep(_PAUSE_SECONDS)
