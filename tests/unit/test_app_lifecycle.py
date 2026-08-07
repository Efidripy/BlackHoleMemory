from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from blackholememory import app as bhm_app


def _patch_lifespan_dependencies(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> list[asyncio.Task]:
    created: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    def track_create_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        created.append(task)
        return task

    async def blocked_task() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(bhm_app.asyncio, "create_task", track_create_task)
    monkeypatch.setattr(bhm_app, "caller_auth_configuration_error", lambda: None)
    monkeypatch.setattr(
        bhm_app,
        "_memory_store_state",
        lambda: SimpleNamespace(configured_mode="sqlite-authoritative", ready=True),
    )
    monkeypatch.setattr(bhm_app, "_wait_for_required_storage_ready", lambda: asyncio.sleep(0))
    monkeypatch.setattr(
        bhm_app,
        "ensure_memory_collections",
        lambda _project: {
            "local": {"collection_name": "local"},
            "global": {"collection_name": "global"},
        },
    )
    monkeypatch.setattr(bhm_app, "warmup_provider_probe", blocked_task)
    monkeypatch.setattr(bhm_app, "_telemetry_harvester_loop", blocked_task)
    monkeypatch.setattr(bhm_app, "_boot_report_is_pending", lambda: False)
    monkeypatch.setattr(bhm_app, "_stop_hook_queue_workers", lambda: _record_event(events, "workers_stopped"))
    monkeypatch.setattr(
        bhm_app,
        "_cleanup_registered_infra_processes",
        lambda **kwargs: _record_event(events, kwargs["reason"]),
    )
    return created


async def _record_event(events: list[str], event: str) -> None:
    events.append(event)


def test_lifespan_cleans_background_tasks_when_worker_startup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    created: list[asyncio.Task] = []

    async def fail_worker_start() -> None:
        raise RuntimeError("synthetic hook worker startup failure")

    async def exercise() -> None:
        nonlocal created
        created = _patch_lifespan_dependencies(monkeypatch, events)
        monkeypatch.setattr(bhm_app, "_start_hook_queue_workers", fail_worker_start)
        manager = bhm_app._app_lifespan(bhm_app.app)
        with pytest.raises(RuntimeError, match="synthetic hook worker startup failure"):
            await manager.__aenter__()

    asyncio.run(exercise())

    assert events == ["workers_stopped", "api_shutdown"]
    assert created
    assert all(task.done() and task.cancelled() for task in created)


def test_lifespan_rejects_non_loopback_listener_before_runtime_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bhm_app.settings, "host", "0.0.0.0")

    async def exercise() -> None:
        manager = bhm_app._app_lifespan(bhm_app.app)
        with pytest.raises(RuntimeError, match="loopback-only"):
            await manager.__aenter__()

    asyncio.run(exercise())


def test_lifespan_shutdown_stops_workers_and_infra_after_context_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    created: list[asyncio.Task] = []

    @asynccontextmanager
    async def fake_mcp_run():
        events.append("mcp_enter")
        yield
        events.append("mcp_exit")

    async def start_workers() -> None:
        events.append("workers_started")

    async def exercise() -> None:
        nonlocal created
        created = _patch_lifespan_dependencies(monkeypatch, events)
        monkeypatch.setattr(bhm_app, "_start_hook_queue_workers", start_workers)
        monkeypatch.setattr(bhm_app._MCP_STREAMABLE_HTTP, "run", fake_mcp_run)
        async with bhm_app._app_lifespan(bhm_app.app):
            events.append("body")

    asyncio.run(exercise())

    assert events == ["workers_started", "mcp_enter", "body", "mcp_exit", "workers_stopped", "api_shutdown"]
    assert created
    assert all(task.done() and task.cancelled() for task in created)


def test_lifespan_observes_failed_background_task_and_cleans_siblings(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    created: list[asyncio.Task] = []

    async def fail_warmup() -> None:
        raise RuntimeError("synthetic provider warmup failure")

    @asynccontextmanager
    async def fake_mcp_run():
        events.append("mcp_enter")
        yield
        events.append("mcp_exit")

    async def start_workers() -> None:
        events.append("workers_started")

    async def exercise() -> None:
        nonlocal created
        created = _patch_lifespan_dependencies(monkeypatch, events)
        monkeypatch.setattr(bhm_app, "warmup_provider_probe", fail_warmup)
        monkeypatch.setattr(bhm_app, "_start_hook_queue_workers", start_workers)
        monkeypatch.setattr(bhm_app._MCP_STREAMABLE_HTTP, "run", fake_mcp_run)
        async with bhm_app._app_lifespan(bhm_app.app):
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            events.append("body")

    asyncio.run(exercise())

    assert events == ["workers_started", "mcp_enter", "body", "mcp_exit", "workers_stopped", "api_shutdown"]
    assert created
    assert all(task.done() and (task.cancelled() or task.exception() is not None) for task in created)


def test_lifespan_cleanup_runs_when_worker_stop_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    created: list[asyncio.Task] = []

    @asynccontextmanager
    async def fake_mcp_run():
        events.append("mcp_enter")
        yield
        events.append("mcp_exit")

    async def start_workers() -> None:
        events.append("workers_started")

    async def fail_stop_workers() -> None:
        raise RuntimeError("synthetic hook worker shutdown failure")

    async def exercise() -> None:
        nonlocal created
        created = _patch_lifespan_dependencies(monkeypatch, events)
        monkeypatch.setattr(bhm_app, "_start_hook_queue_workers", start_workers)
        monkeypatch.setattr(bhm_app, "_stop_hook_queue_workers", fail_stop_workers)
        monkeypatch.setattr(bhm_app._MCP_STREAMABLE_HTTP, "run", fake_mcp_run)
        with pytest.raises(RuntimeError, match="synthetic hook worker shutdown failure"):
            async with bhm_app._app_lifespan(bhm_app.app):
                events.append("body")

    asyncio.run(exercise())

    assert events == ["workers_started", "mcp_enter", "body", "mcp_exit", "api_shutdown"]
    assert created
    assert all(task.done() and task.cancelled() for task in created)
