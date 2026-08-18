"""Pure response builders for the bounded health and SLO contract."""

from __future__ import annotations

from typing import Any, Mapping


def health_live_payload(*, service: str, environment: str) -> dict[str, Any]:
    return {"ok": True, "service": service, "env": environment}


def health_ready_payload(
    *,
    dependency_report: Mapping[str, Any],
    storage: Mapping[str, Any],
    memory_store: Mapping[str, Any],
    fallback_mode: str,
    fallback_active: bool,
    mem0_plan: Mapping[str, Any],
    provider_warmup: Mapping[str, Any],
    projection_required: bool = True,
) -> dict[str, Any]:
    storage_ready = bool(storage["ready"])
    core_ready = (
        bool(dependency_report["ok"])
        and bool(memory_store["ready"])
        and not fallback_active
        and (storage_ready or not projection_required)
    )
    return {
        "ok": core_ready,
        "graph": "compiled",
        "mem0": dict(mem0_plan),
        "storage": dict(storage),
        "memory_store": dict(memory_store),
        "fallback": {"mode": fallback_mode, "active": fallback_active},
        "projection": {
            "required_for_core": bool(projection_required),
            "ready": storage_ready,
            "status": str(storage.get("readiness") or "not-ready"),
        },
        "provider_warmup": dict(provider_warmup),
        "dependencies": list(dependency_report["dependencies"]),
    }


def health_ready_public_payload(*, ready: Mapping[str, Any]) -> dict[str, Any]:
    """Return the minimal anonymous readiness contract."""

    ok = bool(ready.get("ok"))
    return {"ok": ok, "status": "ready" if ok else "not_ready"}


def bhm_health_payload(
    *,
    service: str,
    version: str,
    port: int,
    attach: Mapping[str, Any] | None = None,
    transport: Mapping[str, Any] | None = None,
    storage: Mapping[str, Any],
    memory_store: Mapping[str, Any],
    fallback_mode: str,
    fallback_active: bool,
    observed_at: str | None = None,
    projection_required: bool = True,
) -> dict[str, Any]:
    storage_ready = bool(storage["ready"])
    memory_store_ready = bool(memory_store["ready"])
    if storage_ready and memory_store_ready and not fallback_active:
        status = "healthy"
    elif (
        (memory_store_ready and not projection_required)
        or storage.get("readiness") == "degraded"
        or memory_store.get("readiness") == "degraded"
        or fallback_active
    ):
        status = "degraded"
    else:
        status = "not_ready"
    payload = {
        "status": status,
        "service": service,
        "version": version,
        "viewerPort": port,
        "readyPath": "/health/ready",
        "storage": dict(storage),
        "memory_store": dict(memory_store),
        "fallback": {"mode": fallback_mode, "active": fallback_active},
        "projection": {
            "required_for_core": bool(projection_required),
            "ready": storage_ready,
            "status": str(storage.get("readiness") or "not-ready"),
        },
        "observed_at": observed_at,
    }
    # The legacy heartbeat lease is retired from the public health contract.
    # Streamable HTTP is the sole current MCP lifecycle authority.
    if transport is not None:
        payload["mcp_transport"] = dict(transport)
    return payload


def health_cutover_payload(
    *,
    dependency_report: Mapping[str, Any],
    storage: Mapping[str, Any],
    memory_store: Mapping[str, Any],
    fallback_mode: str,
    fallback_active: bool,
    mem0_plan: Mapping[str, Any],
    projection_required: bool = True,
) -> dict[str, Any]:
    storage_ready = bool(storage["ready"])
    required_ok = (
        bool(dependency_report["required_ok"])
        and bool(memory_store["ready"])
        and not fallback_active
        and (storage_ready or not projection_required)
    )
    return {
        "ok": required_ok,
        "required_ok": required_ok,
        "optional_ok": bool(dependency_report["optional_ok"]),
        "graph": "compiled",
        "mem0": dict(mem0_plan),
        "storage": dict(storage),
        "memory_store": dict(memory_store),
        "fallback": {"mode": fallback_mode, "active": fallback_active},
        "projection": {
            "required_for_core": bool(projection_required),
            "ready": storage_ready,
            "status": str(storage.get("readiness") or "not-ready"),
        },
        "dependencies": list(dependency_report["dependencies"]),
    }


def health_slo_payload(
    *,
    budgets: Mapping[str, Any],
    ready: Mapping[str, Any],
    cutover: Mapping[str, Any],
    provider_warmup: Mapping[str, Any],
    queue_status: Mapping[str, Any],
    outbox: Mapping[str, Any],
    service: str,
    generated_at: str,
) -> dict[str, Any]:
    observed = {
        "runtime_ready": bool(ready.get("ok")),
        "cutover_ready": bool(cutover.get("ok")),
        "provider_ready": bool(provider_warmup.get("ready")),
        "qdrant_healthy": bool(ready.get("storage", {}).get("ready")),
        "hook_queue_pending": int(queue_status.get("pending") or 0),
        "hook_queue_failed": int((queue_status.get("counts") or {}).get("failed") or 0),
        "hook_queue_oldest_age_ms": int(queue_status.get("oldestQueuedAgeMs") or 0),
        "projection_pending": int(outbox.get("pending") or 0) + int(outbox.get("processing") or 0),
        "projection_failed": int(outbox.get("failed") or 0) + int(outbox.get("dead_letter") or 0),
        "outbox": dict(outbox),
    }
    checks = {
        "runtime_ready": observed["runtime_ready"],
        "cutover_ready": observed["cutover_ready"],
        "provider_ready": observed["provider_ready"] or not bool(budgets["require_provider_ready"]),
        "qdrant_healthy": observed["qdrant_healthy"],
        "hook_queue_pending_within_budget": observed["hook_queue_pending"] <= int(budgets["hook_queue_pending"]),
        "hook_queue_failed_within_budget": observed["hook_queue_failed"] <= int(budgets["hook_queue_failed"]),
        "hook_queue_oldest_within_budget": observed["hook_queue_oldest_age_ms"] <= int(budgets["hook_queue_oldest_age_ms"]),
        "projection_pending_within_budget": observed["projection_pending"] <= int(budgets["projection_pending"]),
        "projection_failed_within_budget": observed["projection_failed"] <= int(budgets["projection_failed"]),
    }
    ok = all(checks.values())
    return {
        "ok": ok,
        "status": "healthy" if ok else "breached",
        "service": service,
        "slo_version": 1,
        "generated_at": generated_at,
        "budgets": dict(budgets),
        "observed": observed,
        "checks": checks,
    }
