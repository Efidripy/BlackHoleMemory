"""Runtime orchestration for the bounded HTTP health surfaces.

The public route functions remain in :mod:`blackholememory.app` so their
names, OpenAPI operation ids, and existing import surface stay stable.  This
module owns the health-domain assembly and receives runtime dependencies
explicitly instead of importing the application module back.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .health_contract import bhm_health_payload
from .health_contract import health_cutover_payload
from .health_contract import health_live_payload
from .health_contract import health_ready_payload
from .health_contract import health_ready_public_payload
from .health_contract import health_slo_payload


def _projection_required_by_default() -> bool:
    return True


@dataclass(frozen=True)
class HealthRuntimeDependencies:
    """Callbacks and immutable settings required by the health surfaces."""

    app_name: str
    app_env: str
    runtime_version: str
    port: int
    dependency_report: Callable[..., Mapping[str, Any]]
    storage_runtime_state: Callable[[], Any]
    memory_store_state: Callable[[], Any]
    configured_fallback_mode: Callable[[], str]
    fallback_grace_active: Callable[[], bool]
    mem0_runtime_plan: Callable[[], Mapping[str, Any]]
    provider_warmup_status: Callable[[], Mapping[str, Any]]
    utc_now: Callable[[], str]
    transport_snapshot: Callable[[], Mapping[str, Any]]
    hook_queue_path: Callable[[], Path]
    hook_queue: Callable[[], Any]
    memory_service: Callable[[], Any]
    sqlite_authoritative_mode: str
    memory_service_not_ready: type[Exception]
    projection_required_for_core: Callable[[], bool] = _projection_required_by_default


def _dependency_report_for_core(
    runtime: HealthRuntimeDependencies,
    *,
    projection_required: bool,
) -> dict[str, Any]:
    report = dict(runtime.dependency_report())
    dependencies = [dict(item) for item in report.get("dependencies", [])]
    if not projection_required:
        for item in dependencies:
            if item.get("name") == "qdrant":
                item["required"] = False
    required = [item for item in dependencies if item.get("required", True)]
    optional = [item for item in dependencies if not item.get("required", True)]
    report["dependencies"] = dependencies
    report["required_ok"] = all(bool(item.get("ok")) for item in required)
    report["optional_ok"] = all(bool(item.get("ok")) for item in optional) if optional else True
    report["ok"] = report["required_ok"]
    return report


def build_live(runtime: HealthRuntimeDependencies) -> dict[str, Any]:
    return health_live_payload(service=runtime.app_name, environment=runtime.app_env)


def build_ready(runtime: HealthRuntimeDependencies) -> dict[str, Any]:
    storage = runtime.storage_runtime_state()
    memory_store = runtime.memory_store_state()
    projection_required = (
        runtime.projection_required_for_core()
        or memory_store.configured_mode != runtime.sqlite_authoritative_mode
    )
    dependency_report = _dependency_report_for_core(runtime, projection_required=projection_required)
    return health_ready_payload(
        dependency_report=dependency_report,
        storage=storage.as_dict(),
        memory_store=memory_store.as_dict(),
        fallback_mode=runtime.configured_fallback_mode(),
        fallback_active=runtime.fallback_grace_active(),
        mem0_plan=runtime.mem0_runtime_plan(),
        provider_warmup=runtime.provider_warmup_status(),
        projection_required=projection_required,
    )


def build_ready_public(runtime: HealthRuntimeDependencies) -> dict[str, Any]:
    return health_ready_public_payload(ready=build_ready(runtime))


def build_bhm_health(runtime: HealthRuntimeDependencies) -> dict[str, Any]:
    storage = runtime.storage_runtime_state()
    memory_store = runtime.memory_store_state()
    projection_required = (
        runtime.projection_required_for_core()
        or memory_store.configured_mode != runtime.sqlite_authoritative_mode
    )
    return bhm_health_payload(
        service=runtime.app_name,
        version=runtime.runtime_version,
        port=runtime.port,
        transport=runtime.transport_snapshot(),
        storage=storage.as_dict(),
        memory_store=memory_store.as_dict(),
        fallback_mode=runtime.configured_fallback_mode(),
        fallback_active=runtime.fallback_grace_active(),
        observed_at=runtime.utc_now(),
        projection_required=projection_required,
    )


def build_cutover(runtime: HealthRuntimeDependencies) -> dict[str, Any]:
    storage = runtime.storage_runtime_state()
    memory_store = runtime.memory_store_state()
    projection_required = (
        runtime.projection_required_for_core()
        or memory_store.configured_mode != runtime.sqlite_authoritative_mode
    )
    dependency_report = _dependency_report_for_core(runtime, projection_required=projection_required)
    return health_cutover_payload(
        dependency_report=dependency_report,
        storage=storage.as_dict(),
        memory_store=memory_store.as_dict(),
        fallback_mode=runtime.configured_fallback_mode(),
        fallback_active=runtime.fallback_grace_active(),
        mem0_plan=runtime.mem0_runtime_plan(),
        projection_required=projection_required,
    )


def build_slo(
    runtime: HealthRuntimeDependencies,
    *,
    ready_factory: Callable[[], Mapping[str, Any]],
    cutover_factory: Callable[[], Mapping[str, Any]],
    max_hook_queue_pending: int = 100,
    max_hook_queue_failed: int = 0,
    max_hook_queue_oldest_age_ms: int = 30_000,
    max_projection_pending: int = 0,
    max_projection_failed: int = 0,
    require_provider_ready: bool = True,
) -> dict[str, Any]:
    """Assemble the bounded operator SLO contract from live callbacks."""

    budgets = {
        "hook_queue_pending": max(int(max_hook_queue_pending), 0),
        "hook_queue_failed": max(int(max_hook_queue_failed), 0),
        "hook_queue_oldest_age_ms": max(int(max_hook_queue_oldest_age_ms), 0),
        "projection_pending": max(int(max_projection_pending), 0),
        "projection_failed": max(int(max_projection_failed), 0),
        "require_provider_ready": bool(require_provider_ready),
    }
    ready = ready_factory()
    cutover = cutover_factory()
    warmup = runtime.provider_warmup_status()
    if runtime.hook_queue_path().exists():
        queue_status = runtime.hook_queue().status()
    else:
        queue_status = {
            "pending": 0,
            "counts": {"queued": 0, "processing": 0, "failed": 0},
            "oldestQueuedAgeMs": 0,
        }

    memory_store = runtime.memory_store_state()
    outbox: dict[str, Any] = {
        "available": False,
        "pending": 0,
        "processing": 0,
        "failed": 0,
        "dead_letter": 0,
        "completed": 0,
        "total": 0,
    }
    if memory_store.configured_mode == runtime.sqlite_authoritative_mode:
        try:
            outbox = {"available": True, **runtime.memory_service().outbox_status()}
        except runtime.memory_service_not_ready:
            outbox["error"] = "memory_service_unavailable"

    return health_slo_payload(
        budgets=budgets,
        ready=ready,
        cutover=cutover,
        provider_warmup=warmup,
        queue_status=queue_status,
        outbox=outbox,
        service=runtime.app_name,
        generated_at=runtime.utc_now(),
    )
