from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from blackholememory import app as bhm_app
from blackholememory.health_routes import HealthRuntimeDependencies
from blackholememory.health_routes import build_bhm_health
from blackholememory.health_routes import build_cutover
from blackholememory.health_routes import build_live
from blackholememory.health_routes import build_ready
from blackholememory.health_routes import build_ready_public
from blackholememory.health_routes import build_slo


def _runtime(*, mode: str = "sqlite-authoritative", outbox: dict | None = None) -> HealthRuntimeDependencies:
    state = SimpleNamespace(
        configured_mode=mode,
        as_dict=lambda: {"ready": True, "readiness": "ready", "configured_mode": mode},
    )
    return HealthRuntimeDependencies(
        app_name="BlackHoleMemory",
        app_env="test",
        runtime_version="bhm-test",
        port=8000,
        dependency_report=lambda **_kwargs: {
            "ok": True,
            "required_ok": True,
            "optional_ok": True,
            "dependencies": [{"name": "qdrant", "ok": True}],
        },
        storage_runtime_state=lambda: state,
        memory_store_state=lambda: state,
        configured_fallback_mode=lambda: "explicit",
        fallback_grace_active=lambda: False,
        mem0_runtime_plan=lambda: {"status": "projection-only"},
        provider_warmup_status=lambda: {"ready": True},
        utc_now=lambda: "2026-07-30T00:00:00Z",
        transport_snapshot=lambda: {"status": "attached"},
        hook_queue_path=lambda: Path("missing-hook-queue.sqlite3"),
        hook_queue=lambda: SimpleNamespace(status=lambda: {}),
        memory_service=lambda: SimpleNamespace(outbox_status=lambda: outbox or {"pending": 0, "processing": 0, "failed": 0, "dead_letter": 0, "completed": 1, "total": 1}),
        sqlite_authoritative_mode="sqlite-authoritative",
        memory_service_not_ready=RuntimeError,
    )


def test_health_domain_builders_keep_route_contract_shapes() -> None:
    runtime = _runtime()

    assert build_live(runtime) == {"ok": True, "service": "BlackHoleMemory", "env": "test"}
    ready = build_ready(runtime)
    assert ready["ok"] is True
    assert build_ready_public(runtime) == {"ok": True, "status": "ready"}
    assert build_bhm_health(runtime)["mcp_transport"]["status"] == "attached"
    assert build_cutover(runtime)["required_ok"] is True


def test_health_domain_slo_uses_supplied_route_factories() -> None:
    runtime = _runtime(outbox={"pending": 2, "processing": 0, "failed": 1, "dead_letter": 0, "completed": 4, "total": 7})
    result = build_slo(
        runtime,
        ready_factory=lambda: {"ok": True, "storage": {"ready": True}},
        cutover_factory=lambda: {"ok": True},
        max_projection_pending=0,
        max_projection_failed=0,
    )

    assert result["status"] == "breached"
    assert result["checks"]["projection_pending_within_budget"] is False
    assert result["checks"]["projection_failed_within_budget"] is False


def test_http_health_route_catalog_and_import_surface_are_stable() -> None:
    expected = {
        ("GET", "/health/live", "health_live"),
        ("GET", "/health/dependencies", "health_dependencies"),
        ("GET", "/health/ready", "health_ready_endpoint"),
        ("GET", "/bhm/health", "bhm_health"),
        ("GET", "/health/cutover", "health_cutover"),
        ("GET", "/bhm/health/slo", "bhm_health_slo"),
    }
    actual = {
        (method, route.path, route.endpoint.__name__)
        for route in bhm_app.app.routes
        if route.path in {item[1] for item in expected}
        for method in sorted(getattr(route, "methods", set()))
    }

    assert actual == expected
    assert bhm_app.health_live.__name__ == "health_live"
    assert bhm_app.health_ready_endpoint.__name__ == "health_ready_endpoint"
    assert bhm_app.bhm_health_slo.__name__ == "bhm_health_slo"
