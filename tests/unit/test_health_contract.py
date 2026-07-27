from __future__ import annotations

from blackholememory.health_contract import bhm_health_payload
from blackholememory.health_contract import health_cutover_payload
from blackholememory.health_contract import health_live_payload
from blackholememory.health_contract import health_ready_payload
from blackholememory.health_contract import health_slo_payload


def _dependency_report() -> dict:
    return {"ok": True, "required_ok": True, "optional_ok": True, "dependencies": [{"name": "qdrant", "ok": True}]}


def _storage() -> dict:
    return {"ready": True, "readiness": "ready"}


def _memory_store() -> dict:
    return {"ready": True, "readiness": "ready", "backend": "sqlite-authoritative"}


def test_health_contract_builders_preserve_public_shapes():
    live = health_live_payload(service="BlackHoleMemory", environment="test")
    ready = health_ready_payload(
        dependency_report=_dependency_report(),
        storage=_storage(),
        memory_store=_memory_store(),
        fallback_mode="explicit",
        fallback_active=False,
        mem0_plan={"status": "projection-only"},
        provider_warmup={"ready": True},
    )
    health = bhm_health_payload(
        service="BlackHoleMemory",
        version="bhm-v1.7.1-PURE",
        port=8000,
        attach={"status": "detached"},
        storage=_storage(),
        memory_store=_memory_store(),
        fallback_mode="explicit",
        fallback_active=False,
    )
    cutover = health_cutover_payload(
        dependency_report=_dependency_report(),
        storage=_storage(),
        memory_store=_memory_store(),
        fallback_mode="explicit",
        fallback_active=False,
        mem0_plan={"status": "projection-only"},
    )

    assert live == {"ok": True, "service": "BlackHoleMemory", "env": "test"}
    assert ready["ok"] is True
    assert health["status"] == "healthy"
    assert health["memory_store"]["backend"] == "sqlite-authoritative"
    assert cutover["ok"] is True and cutover["required_ok"] is True


def test_health_contract_exposes_streamable_transport_as_the_only_mcp_truth():
    health = bhm_health_payload(
        service="BlackHoleMemory",
        version="bhm-v1.7.1-PURE",
        port=8000,
        transport={
            "status": "attached",
            "authoritative_source": "streamable_http_sessions",
            "attached_count": 1,
        },
        storage=_storage(),
        memory_store=_memory_store(),
        fallback_mode="explicit",
        fallback_active=False,
    )

    assert health["mcp_transport"]["status"] == "attached"
    assert health["mcp_transport"]["authoritative_source"] == "streamable_http_sessions"
    assert "mcp_attach" not in health


def test_health_slo_builder_fails_closed_on_budget_breach():
    result = health_slo_payload(
        budgets={
            "hook_queue_pending": 0,
            "hook_queue_failed": 0,
            "hook_queue_oldest_age_ms": 0,
            "projection_pending": 0,
            "projection_failed": 0,
            "require_provider_ready": True,
        },
        ready={"ok": True, "storage": {"ready": True}},
        cutover={"ok": True},
        provider_warmup={"ready": True},
        queue_status={"pending": 1, "counts": {"failed": 0}, "oldestQueuedAgeMs": 10},
        outbox={"pending": 0, "processing": 0, "failed": 0, "dead_letter": 0},
        service="BlackHoleMemory",
        generated_at="2026-07-14T00:00:00Z",
    )

    assert result["ok"] is False
    assert result["status"] == "breached"
    assert result["checks"]["hook_queue_pending_within_budget"] is False
