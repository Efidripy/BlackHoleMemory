from __future__ import annotations

from types import SimpleNamespace

from blackholememory import app as bhm_app
from blackholememory import bhm_mcp


def _healthy_state():
    return SimpleNamespace(
        configured_mode="sqlite-authoritative",
        ready=True,
        as_dict=lambda: {"ready": True, "configured_mode": "sqlite-authoritative"},
    )


def test_health_slo_reports_healthy_bounded_runtime(monkeypatch):
    monkeypatch.setattr(bhm_app, "health_ready", lambda: {"ok": True, "storage": {"ready": True}})
    monkeypatch.setattr(bhm_app, "health_cutover", lambda: {"ok": True})
    monkeypatch.setattr(bhm_app, "_get_provider_warmup_status", lambda: {"ready": True})
    monkeypatch.setattr(bhm_app, "_memory_store_state", _healthy_state)
    monkeypatch.setattr(bhm_app, "_hook_queue_path", lambda: SimpleNamespace(exists=lambda: False))
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: SimpleNamespace(outbox_status=lambda: {"pending": 0, "processing": 0, "failed": 0, "dead_letter": 0, "completed": 3, "total": 3}))

    result = bhm_app.bhm_health_slo()

    assert result["ok"] is True
    assert result["status"] == "healthy"
    assert all(result["checks"].values())
    assert result["observed"]["projection_pending"] == 0


def test_health_slo_breaches_projection_budget(monkeypatch):
    monkeypatch.setattr(bhm_app, "health_ready", lambda: {"ok": True, "storage": {"ready": True}})
    monkeypatch.setattr(bhm_app, "health_cutover", lambda: {"ok": True})
    monkeypatch.setattr(bhm_app, "_get_provider_warmup_status", lambda: {"ready": True})
    monkeypatch.setattr(bhm_app, "_memory_store_state", _healthy_state)
    monkeypatch.setattr(bhm_app, "_hook_queue_path", lambda: SimpleNamespace(exists=lambda: False))
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: SimpleNamespace(outbox_status=lambda: {"pending": 2, "processing": 1, "failed": 1, "dead_letter": 0, "completed": 3, "total": 7}))

    result = bhm_app.bhm_health_slo(max_projection_pending=0, max_projection_failed=0)

    assert result["ok"] is False
    assert result["status"] == "breached"
    assert result["checks"]["projection_pending_within_budget"] is False
    assert result["checks"]["projection_failed_within_budget"] is False


def test_health_slo_mcp_wrapper_uses_read_only_route(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        bhm_mcp,
        "_get",
        lambda path, params: calls.append((path, params)) or {"ok": True},
    )

    assert bhm_mcp.bhm_health_slo(max_projection_pending=2) == {"ok": True}
    assert calls == [
        (
            "/bhm/health/slo",
            {
                "max_hook_queue_pending": 100,
                "max_hook_queue_failed": 0,
                "max_hook_queue_oldest_age_ms": 30_000,
                "max_projection_pending": 2,
                "max_projection_failed": 0,
                "require_provider_ready": True,
            },
        )
    ]
