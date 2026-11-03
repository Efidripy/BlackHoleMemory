from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import logging

import pytest

from blackholememory import app as bhm_app
from blackholememory import app_core, app_lifecycle, app_runtime


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
        app_runtime,
        "_memory_store_state",
        lambda: SimpleNamespace(configured_mode="sqlite-authoritative", ready=True),
    )
    monkeypatch.setattr(app_lifecycle, "_wait_for_required_storage_ready", lambda: asyncio.sleep(0))
    monkeypatch.setattr(
        bhm_app,
        "ensure_memory_collections",
        lambda _project: {
            "local": {"collection_name": "local"},
            "global": {"collection_name": "global"},
        },
    )
    monkeypatch.setattr(app_runtime, "warmup_provider_probe", blocked_task)
    monkeypatch.setattr(app_lifecycle, "_telemetry_harvester_loop", blocked_task)
    monkeypatch.setattr(app_lifecycle, "_boot_report_is_pending", lambda: False)
    monkeypatch.setattr(app_lifecycle, "_stop_hook_queue_workers", lambda: _record_event(events, "workers_stopped"))
    monkeypatch.setattr(
        app_lifecycle,
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
        monkeypatch.setattr(app_lifecycle, "_start_hook_queue_workers", fail_worker_start)
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
        monkeypatch.setattr(app_lifecycle, "_start_hook_queue_workers", start_workers)
        monkeypatch.setattr(app_core._MCP_STREAMABLE_HTTP, "run", fake_mcp_run)
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
        monkeypatch.setattr(app_runtime, "warmup_provider_probe", fail_warmup)
        monkeypatch.setattr(app_lifecycle, "_start_hook_queue_workers", start_workers)
        monkeypatch.setattr(app_core._MCP_STREAMABLE_HTTP, "run", fake_mcp_run)
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
        monkeypatch.setattr(app_lifecycle, "_start_hook_queue_workers", start_workers)
        monkeypatch.setattr(app_lifecycle, "_stop_hook_queue_workers", fail_stop_workers)
        monkeypatch.setattr(app_core._MCP_STREAMABLE_HTTP, "run", fake_mcp_run)
        with pytest.raises(RuntimeError, match="synthetic hook worker shutdown failure"):
            async with bhm_app._app_lifespan(bhm_app.app):
                events.append("body")

    asyncio.run(exercise())

    assert events == ["workers_started", "mcp_enter", "body", "mcp_exit", "api_shutdown"]
    assert created
    assert all(task.done() and task.cancelled() for task in created)


def test_startup_warns_on_active_binding_dimension_mismatch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    binding1 = SimpleNamespace(project="proj-mismatch-1", vector_dimensions=1024)
    binding2 = SimpleNamespace(project="proj-mismatch-2", vector_dimensions=512)

    monkeypatch.setattr(
        bhm_app,
        "active_projection_generation_bindings",
        lambda _db: (binding1, binding2),
    )
    monkeypatch.setattr(bhm_app.settings, "mem0_embedding_dims", 768)
    monkeypatch.delenv("BHM_MEM0_EMBEDDING_DIMS", raising=False)

    with caplog.at_level(logging.WARNING, logger="blackholememory.app"):
        bhm_app._check_active_project_dimensions()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "proj-mismatch-1" in warnings[0].message
    assert "proj-mismatch-2" not in warnings[0].message
    assert "1024" in warnings[0].message
    assert "768" in warnings[0].message


def test_startup_does_not_warn_when_dimensions_match(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    binding = SimpleNamespace(project="proj-matching", vector_dimensions=768)

    monkeypatch.setattr(
        bhm_app,
        "active_projection_generation_bindings",
        lambda _db: (binding,),
    )
    monkeypatch.setattr(bhm_app.settings, "mem0_embedding_dims", 768)
    monkeypatch.delenv("BHM_MEM0_EMBEDDING_DIMS", raising=False)

    with caplog.at_level(logging.WARNING, logger="blackholememory.app"):
        bhm_app._check_active_project_dimensions()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 0


def test_startup_check_dimension_error_does_not_crash(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def failing_bindings(_db):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(
        bhm_app,
        "active_projection_generation_bindings",
        failing_bindings,
    )

    with caplog.at_level(logging.WARNING, logger="blackholememory.app"):
        bhm_app._check_active_project_dimensions()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "Could not verify" in warnings[0].message


def test_lifespan_emits_dimension_warning_without_blocking_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    events: list[str] = []
    _patch_lifespan_dependencies(monkeypatch, events)

    binding = SimpleNamespace(project="proj-mismatch-lifespan", vector_dimensions=1024)
    monkeypatch.setattr(
        bhm_app,
        "active_projection_generation_bindings",
        lambda _db: (binding,),
    )
    monkeypatch.setattr(bhm_app.settings, "mem0_embedding_dims", 768)
    monkeypatch.delenv("BHM_MEM0_EMBEDDING_DIMS", raising=False)

    @asynccontextmanager
    async def fake_mcp_run():
        events.append("mcp_running")
        yield

    monkeypatch.setattr(app_core._MCP_STREAMABLE_HTTP, "run", fake_mcp_run)

    async def fake_start_workers():
        events.append("workers_started")

    monkeypatch.setattr(app_lifecycle, "_start_hook_queue_workers", fake_start_workers)

    async def exercise() -> None:
        async with bhm_app._app_lifespan(bhm_app.app):
            events.append("lifespan_active")

    with caplog.at_level(logging.WARNING, logger="blackholememory.app"):
        asyncio.run(exercise())

    assert "lifespan_active" in events
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "proj-mismatch-lifespan" in r.message]
    assert len(warnings) == 1
