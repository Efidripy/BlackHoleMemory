from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from blackholememory import app as bhm_app
from blackholememory.mem0_adapter import StorageNotReady


def _projection_state(*, mode: str, readiness: str, reason: str) -> SimpleNamespace:
    ready = readiness == "ready"
    payload = {
        "configured_mode": mode,
        "remote_available": ready,
        "backend": "remote" if ready else "unavailable",
        "readiness": readiness,
        "reason": reason,
        "ready": ready,
    }
    return SimpleNamespace(
        configured_mode=mode,
        readiness=readiness,
        reason=reason,
        ready=ready,
        as_dict=lambda: dict(payload),
    )


def _authoritative_memory_state() -> SimpleNamespace:
    payload = {
        "configured_mode": "sqlite-authoritative",
        "backend": "sqlite-authoritative",
        "readiness": "ready",
        "reason": "sqlite_authoritative_guard_passed",
        "ready": True,
    }
    return SimpleNamespace(
        configured_mode="sqlite-authoritative",
        readiness="ready",
        reason="sqlite_authoritative_guard_passed",
        ready=True,
        as_dict=lambda: dict(payload),
    )


def _optional_qdrant_down_report(**_kwargs) -> dict:
    return {
        "ok": True,
        "required_ok": True,
        "optional_ok": False,
        "dependencies": [
            {
                "name": "qdrant",
                "ok": False,
                "detail": "qdrant_unavailable",
                "required": False,
            }
        ],
    }


def _required_qdrant_down_report(**_kwargs) -> dict:
    return {
        "ok": False,
        "required_ok": False,
        "optional_ok": True,
        "dependencies": [
            {
                "name": "qdrant",
                "ok": False,
                "detail": "qdrant_unavailable",
                "required": True,
            }
        ],
    }


def test_sqlite_authoritative_readiness_stays_ready_when_projection_is_down(monkeypatch) -> None:
    projection = _projection_state(
        mode="remote-required",
        readiness="not-ready",
        reason="remote_qdrant_required_but_unavailable",
    )
    monkeypatch.setenv("BHM_QDRANT_REQUIRED_FOR_CORE", "false")
    monkeypatch.setattr(bhm_app, "storage_runtime_state", lambda: projection)
    monkeypatch.setattr(bhm_app, "_memory_store_state", _authoritative_memory_state)
    monkeypatch.setattr(bhm_app, "dependency_report", _optional_qdrant_down_report)
    monkeypatch.setattr(bhm_app, "_fallback_grace_active", lambda: False)
    monkeypatch.setattr(bhm_app, "mem0_runtime_plan", lambda: {"status": "projection-degraded"})
    monkeypatch.setattr(bhm_app, "_get_provider_warmup_status", lambda: {"ready": True})

    detailed_ready = bhm_app.health_ready()
    public_ready = bhm_app.health_ready_endpoint()

    assert detailed_ready["ok"] is True
    assert detailed_ready["memory_store"]["ready"] is True
    assert detailed_ready["storage"]["ready"] is False
    assert detailed_ready["storage"]["readiness"] == "not-ready"
    assert detailed_ready["projection"] == {
        "required_for_core": False,
        "ready": False,
        "status": "not-ready",
    }
    assert detailed_ready["dependencies"] == _optional_qdrant_down_report()["dependencies"]
    assert public_ready == {"ok": True, "status": "ready"}


def test_detailed_health_reports_projection_degradation_without_losing_sqlite_authority(monkeypatch) -> None:
    projection = _projection_state(
        mode="remote-required",
        readiness="not-ready",
        reason="remote_qdrant_required_but_unavailable",
    )
    monkeypatch.setenv("BHM_QDRANT_REQUIRED_FOR_CORE", "false")
    monkeypatch.setattr(bhm_app, "storage_runtime_state", lambda: projection)
    monkeypatch.setattr(bhm_app, "_memory_store_state", _authoritative_memory_state)
    monkeypatch.setattr(bhm_app, "_fallback_grace_active", lambda: False)
    monkeypatch.setattr(
        bhm_app._MCP_STREAMABLE_HTTP,
        "contract_snapshot",
        lambda: {"sessions": {"status": "attached"}},
    )

    health = bhm_app.bhm_health()

    assert health["status"] == "degraded"
    assert health["memory_store"]["backend"] == "sqlite-authoritative"
    assert health["memory_store"]["ready"] is True
    assert health["storage"] == projection.as_dict()
    assert health["storage"]["reason"] == "remote_qdrant_required_but_unavailable"
    assert health["projection"] == {
        "required_for_core": False,
        "ready": False,
        "status": "not-ready",
    }


def test_sqlite_authoritative_lifespan_enters_when_qdrant_collection_probe_fails(monkeypatch) -> None:
    projection = _projection_state(
        mode="remote-required",
        readiness="not-ready",
        reason="remote_qdrant_required_but_unavailable",
    )
    monkeypatch.setenv("BHM_QDRANT_REQUIRED_FOR_CORE", "false")
    events: list[str] = []

    @asynccontextmanager
    async def transport_run():
        events.append("transport-entered")
        try:
            yield
        finally:
            events.append("transport-exited")

    async def noop_async(*_args, **_kwargs) -> None:
        return None

    def qdrant_unavailable(*_args, **_kwargs):
        raise OSError("qdrant unavailable")

    monkeypatch.setattr(bhm_app, "validate_loopback_listener_host", lambda _host: None)
    monkeypatch.setattr(bhm_app, "caller_auth_configuration_error", lambda: None)
    monkeypatch.setattr(bhm_app, "_memory_store_state", _authoritative_memory_state)
    monkeypatch.setattr(bhm_app, "_initialize_authoritative_memory_service", lambda: None)
    monkeypatch.setattr(bhm_app, "storage_runtime_state", lambda: projection)
    monkeypatch.setattr(bhm_app, "ensure_memory_collections", qdrant_unavailable)
    monkeypatch.setattr(bhm_app, "warmup_provider_probe", noop_async)
    monkeypatch.setattr(bhm_app, "_boot_report_is_pending", lambda: False)
    monkeypatch.setattr(bhm_app, "_telemetry_harvester_loop", noop_async)
    monkeypatch.setattr(bhm_app, "_start_hook_queue_workers", noop_async)
    monkeypatch.setattr(bhm_app, "_stop_hook_queue_workers", noop_async)
    monkeypatch.setattr(bhm_app, "_cleanup_registered_infra_processes", noop_async)
    monkeypatch.setattr(bhm_app._MCP_STREAMABLE_HTTP, "run", transport_run)

    async def enter_and_exit_lifespan() -> None:
        async with bhm_app._app_lifespan(bhm_app.app):
            events.append("application-ready")

    asyncio.run(enter_and_exit_lifespan())

    assert events == ["transport-entered", "application-ready", "transport-exited"]


def test_remote_required_profile_remains_fail_closed_when_qdrant_is_down(monkeypatch) -> None:
    projection = _projection_state(
        mode="remote-required",
        readiness="not-ready",
        reason="remote_qdrant_required_but_unavailable",
    )
    monkeypatch.setenv("BHM_QDRANT_REQUIRED_FOR_CORE", "true")
    monkeypatch.setattr(bhm_app, "storage_runtime_state", lambda: projection)
    monkeypatch.setattr(bhm_app, "_memory_store_state", _authoritative_memory_state)
    monkeypatch.setattr(bhm_app, "dependency_report", _required_qdrant_down_report)
    monkeypatch.setattr(bhm_app, "_fallback_grace_active", lambda: False)
    monkeypatch.setattr(bhm_app, "mem0_runtime_plan", lambda: {"status": "projection-required"})
    monkeypatch.setattr(bhm_app, "_get_provider_warmup_status", lambda: {"ready": True})

    detailed_ready = bhm_app.health_ready()

    assert detailed_ready["ok"] is False
    assert detailed_ready["projection"] == {
        "required_for_core": True,
        "ready": False,
        "status": "not-ready",
    }
    assert bhm_app.health_ready_endpoint() == {"ok": False, "status": "not_ready"}

    with pytest.raises(
        StorageNotReady,
        match="remote_qdrant_required_but_unavailable",
    ):
        asyncio.run(bhm_app._wait_for_required_storage_ready(timeout_seconds=0))


def test_sqlite_authoritative_lifespan_still_fails_closed_in_remote_required_profile(monkeypatch) -> None:
    projection = _projection_state(
        mode="remote-required",
        readiness="not-ready",
        reason="remote_qdrant_required_but_unavailable",
    )
    monkeypatch.setenv("BHM_QDRANT_REQUIRED_FOR_CORE", "true")
    transport_entered = False

    @asynccontextmanager
    async def transport_run():
        nonlocal transport_entered
        transport_entered = True
        yield

    async def noop_async(*_args, **_kwargs) -> None:
        return None

    original_storage_gate = bhm_app._wait_for_required_storage_ready

    async def immediate_strict_storage_gate():
        return await original_storage_gate(timeout_seconds=0)

    monkeypatch.setattr(bhm_app, "validate_loopback_listener_host", lambda _host: None)
    monkeypatch.setattr(bhm_app, "caller_auth_configuration_error", lambda: None)
    monkeypatch.setattr(bhm_app, "_memory_store_state", _authoritative_memory_state)
    monkeypatch.setattr(bhm_app, "_initialize_authoritative_memory_service", lambda: None)
    monkeypatch.setattr(bhm_app, "storage_runtime_state", lambda: projection)
    monkeypatch.setattr(bhm_app, "_wait_for_required_storage_ready", immediate_strict_storage_gate)
    monkeypatch.setattr(
        bhm_app,
        "ensure_memory_collections",
        lambda *_args, **_kwargs: pytest.fail("collection initialization must not run before the strict gate"),
    )
    monkeypatch.setattr(bhm_app, "warmup_provider_probe", noop_async)
    monkeypatch.setattr(bhm_app, "_boot_report_is_pending", lambda: False)
    monkeypatch.setattr(bhm_app, "_telemetry_harvester_loop", noop_async)
    monkeypatch.setattr(bhm_app, "_start_hook_queue_workers", noop_async)
    monkeypatch.setattr(bhm_app, "_stop_hook_queue_workers", noop_async)
    monkeypatch.setattr(bhm_app, "_cleanup_registered_infra_processes", noop_async)
    monkeypatch.setattr(bhm_app._MCP_STREAMABLE_HTTP, "run", transport_run)

    async def enter_lifespan() -> None:
        async with bhm_app._app_lifespan(bhm_app.app):
            pytest.fail("strict remote-required profile must not enter application lifespan")

    with pytest.raises(
        StorageNotReady,
        match="remote_qdrant_required_but_unavailable",
    ):
        asyncio.run(enter_lifespan())

    assert transport_entered is False
