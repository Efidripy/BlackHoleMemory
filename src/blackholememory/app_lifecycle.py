"""Domain module app_lifecycle: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import asyncio
import ctypes
import logging
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi import Request

from . import app_core
from . import app_core_fallback
from . import app_core_hook
from . import app_runtime as rt
from .app_lifecycle_hooks import (
    _HOOK_QUEUE_BOOT_ID as _HOOK_QUEUE_BOOT_ID,  # noqa: F401
    _HOOK_QUEUE_POLL_SECONDS as _HOOK_QUEUE_POLL_SECONDS,  # noqa: F401
    _HOOK_QUEUE_RETRY_BASE_SECONDS as _HOOK_QUEUE_RETRY_BASE_SECONDS,  # noqa: F401
    _HOOK_SCRIPT_MODULE_CACHE as _HOOK_SCRIPT_MODULE_CACHE,  # noqa: F401
    _HOOK_SCRIPT_MODULE_LOCK as _HOOK_SCRIPT_MODULE_LOCK,  # noqa: F401
    _HOOK_TRANSIT_ITEM_CHAR_LIMIT as _HOOK_TRANSIT_ITEM_CHAR_LIMIT,  # noqa: F401
    _append_hook_observation as _append_hook_observation,  # noqa: F401
    _apply_idle_duplicate_merge as _apply_idle_duplicate_merge,  # noqa: F401
    _build_hook_crystallize_request as _build_hook_crystallize_request,  # noqa: F401
    _execute_hook_job as _execute_hook_job,  # noqa: F401
    _handle_compact_hook as _handle_compact_hook,  # noqa: F401
    _hook_compact_concepts as _hook_compact_concepts,  # noqa: F401
    _hook_compact_files as _hook_compact_files,  # noqa: F401
    _hook_item_text as _hook_item_text,  # noqa: F401
    _hook_job_heartbeat as _hook_job_heartbeat,  # noqa: F401
    _hook_job_log_context as _hook_job_log_context,  # noqa: F401
    _hook_job_result_summary as _hook_job_result_summary,  # noqa: F401
    _hook_queue_worker as _hook_queue_worker,  # noqa: F401
    _hook_text_or_none as _hook_text_or_none,  # noqa: F401
    _hook_timestamp as _hook_timestamp,  # noqa: F401
    _materialize_hook_transit_sources as _materialize_hook_transit_sources,  # noqa: F401
    _merge_memories as _merge_memories,  # noqa: F401
    _refresh_decay_scores_for_project as _refresh_decay_scores_for_project,  # noqa: F401
    _repo_script_module as _repo_script_module,  # noqa: F401
    _run_idle_graph_healer as _run_idle_graph_healer,  # noqa: F401
    _run_idle_reflection_daemon as _run_idle_reflection_daemon,  # noqa: F401
    _run_idle_reflection_pipeline as _run_idle_reflection_pipeline,  # noqa: F401
    _start_hook_queue_workers as _start_hook_queue_workers,  # noqa: F401
    _stop_hook_queue_workers as _stop_hook_queue_workers,  # noqa: F401
    _with_context_tier_lifecycle_receipt as _with_context_tier_lifecycle_receipt,  # noqa: F401
)
from .app_runtime import _MAX_CONCURRENT_WRITES, _env_float, _safe_exception_text, _write_json_atomic
from .local_endpoint_policy import open_local_url
from .mcp_surfaces import resolve_mcp_surface
from .mem0_adapter import (
    StorageNotReady,
    _remote_qdrant_available,
    storage_runtime_state,
)
from .resource_limits import (
    PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS,
    PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS,
    PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS,
    QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS,
)
from .usage_telemetry import normalize_operation

_LOGGER = logging.getLogger(__name__)


_STORAGE_STARTUP_TIMEOUT_SECONDS = _env_float(
    "BHM_STORAGE_STARTUP_TIMEOUT_SECONDS",
    30.0,
    0.1,
    PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS,
)


_QDRANT_HEALTH_TIMEOUT_SECONDS = _env_float(
    "BHM_QDRANT_HEALTH_TIMEOUT_SECONDS",
    QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS,
    0.1,
    QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS,
)


_KNOWLEDGE_COUNTERS_SCHEMA_VERSION = "bhm.dashboard.knowledge-counters.v1"


_DASHBOARD_STATUS_SCHEMA_VERSION = "bhm.dashboard.status.v1"


_ARCHITECTURE_MEMORY_TYPES = frozenset({"architecture", "adr"})


_ARCHITECTURE_TAXONOMY_TAGS = frozenset({"architecture", "law", "adr"})


_BOOT_REPORT_QDRANT_POLL_SECONDS = 0.25



def _optional_psutil():
    try:
        import psutil

        return psutil
    except ImportError:
        return None


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _current_working_set_mb() -> float:
    psutil = _optional_psutil()
    if psutil is not None:
        return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1)
    if os.name == "nt":
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        if ok:
            return round(counters.WorkingSetSize / (1024 * 1024), 1)
    return 0.0


def _wsl_shared_overhead_gb() -> float:
    psutil = _optional_psutil()
    if psutil is None:
        return 0.0
    total = 0
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            name = str(proc.info.get("name") or "").lower()
            if name in {"vmmemwsl.exe", "vmmemwsl", "vmmem.exe", "vmmem"}:
                total += int(proc.info["memory_info"].rss)
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue
    return round(total / (1024 * 1024 * 1024), 2)


def _qdrant_healthy_sync() -> bool:
    return _remote_qdrant_available(opener=open_local_url, timeout=_QDRANT_HEALTH_TIMEOUT_SECONDS)


async def _wait_for_required_storage_ready(timeout_seconds: float | None = None):
    timeout = _STORAGE_STARTUP_TIMEOUT_SECONDS if timeout_seconds is None else max(float(timeout_seconds), 0.0)
    deadline = time.monotonic() + timeout
    while True:
        state = await asyncio.to_thread(storage_runtime_state)
        if state.ready or state.configured_mode != "remote-required":
            return state
        if time.monotonic() >= deadline:
            raise StorageNotReady(
                f"required storage did not become ready within {timeout:.1f}s: {state.reason}"
            )
        await asyncio.sleep(_BOOT_REPORT_QDRANT_POLL_SECONDS)


async def _wait_for_qdrant_ready() -> None:
    await _wait_for_required_storage_ready()


def _is_architecture_decision(record: Mapping[str, Any]) -> bool:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    memory_type = str(record.get("memory_type") or metadata.get("memory_type") or "").strip().lower()
    if memory_type in _ARCHITECTURE_MEMORY_TYPES:
        return True
    tags = record.get("tags") or metadata.get("tags") or []
    concepts = {str(item).strip().lower() for item in tags if str(item).strip()}
    semantic_type = str(metadata.get("semantic_type") or "").strip().lower()
    return semantic_type == "decision-log" and bool(_ARCHITECTURE_TAXONOMY_TAGS & concepts)


def _knowledge_counter_group(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"active_count": 0, "archived_count": 0, "tombstoned_count": 0}
    for record in records:
        counts[f"{app_core._memory_lifecycle(record)}_count"] += 1
    return {**counts, "total_count": sum(counts.values())}


def _knowledge_counters_sync(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    source_records = records if records is not None else rt._load_live_memories()
    architecture_records = [record for record in source_records if _is_architecture_decision(record)]
    projects = sorted({str(record.get("project") or "") for record in source_records if record.get("project")})
    return {
        "schema_version": _KNOWLEDGE_COUNTERS_SCHEMA_VERSION,
        "source": {
            "authority": "sqlite",
            "table": "memories",
            "projection_used": False,
        },
        "scope": {
            "kind": "all-local-projects",
            "project_filter": None,
            "project_count": len(projects),
        },
        "memory_records": {
            "label": "Active memory records",
            **_knowledge_counter_group(source_records),
            "definition": "All SQLite memory records grouped by lifecycle.",
        },
        "architecture_decisions": {
            "label": "Active architecture decisions",
            **_knowledge_counter_group(architecture_records),
            "definition": (
                "SQLite memory records classified as architecture or ADR by memory type, "
                "or as decision-log records with architecture taxonomy tags."
            ),
        },
    }


def _collect_host_telemetry_sync() -> dict[str, Any]:
    queue_status = (
        app_core_hook._hook_queue().status()
        if app_core_hook._hook_queue_path().exists()
        else {
            "pending": 0,
            "capacity": app_core_hook._HOOK_QUEUE_CAPACITY,
            "counts": {"queued": 0, "processing": 0, "completed": 0, "failed": 0},
            "oldestQueuedAgeMs": 0,
        }
    )
    return {
        "bhm_working_set_mb": _current_working_set_mb(),
        "wsl_shared_overhead_gb": _wsl_shared_overhead_gb(),
        "qdrant_healthy": _qdrant_healthy_sync(),
        "knowledge_counters": _knowledge_counters_sync(),
        "hook_queue_pending": queue_status["pending"],
        "hook_queue_capacity": queue_status["capacity"],
        "hook_queue_queued": queue_status["counts"]["queued"],
        "hook_queue_processing": queue_status["counts"]["processing"],
        "hook_queue_failed": queue_status["counts"]["failed"],
        "hook_queue_oldest_age_ms": queue_status["oldestQueuedAgeMs"],
    }


async def _collect_sys_status_payload() -> dict[str, Any]:
    host = await asyncio.to_thread(_collect_host_telemetry_sync)
    warmup = rt._get_provider_warmup_status()
    return {
        "event": "sys_status",
        "data": {
            "schema_version": _DASHBOARD_STATUS_SCHEMA_VERSION,
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mcp_active_pipes": 0,
            "mcp_max_instances": 0,
            "mcp_surface": resolve_mcp_surface().value,
            "mcp_max_frame_bytes": 0,
            "mcp_client_timeout_seconds": 0.0,
            "mcp_dispatch_timeout_seconds": 0.0,
            "mcp_transport": app_core._MCP_STREAMABLE_HTTP.contract_snapshot()["sessions"],
            "storage": storage_runtime_state().as_dict(),
            "memory_store": rt._memory_store_state().as_dict(),
            "fallback_mode": app_core_fallback._configured_fallback_mode(),
            "backpressure_semaphore_value": getattr(rt._WRITE_SEMAPHORE, "_value", 0),
            "backpressure_max": _MAX_CONCURRENT_WRITES,
            "fallback_grace_active": app_core_fallback._fallback_grace_active(),
            "launcher_circuit_breaker_status": "STREAMABLE_HTTP",
            "llm_warmup_status": "READY" if warmup.get("ready") else str(warmup.get("phase") or "WARMING").upper(),
            **host,
        },
    }


async def _telemetry_harvester_loop() -> None:
    while True:
        try:
            await asyncio.sleep(app_core_fallback._TELEMETRY_INTERVAL_SECONDS)
            if rt._MEMORY_PULSE_BUS.client_count == 0:
                continue
            await rt._MEMORY_PULSE_BUS.broadcast(await _collect_sys_status_payload())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # broad: telemetry tick must not terminate scheduler
            _LOGGER.warning("Telemetry harvester tick skipped (%s)", type(exc).__name__)


MAX_UI_EXCHANGE_BODY_BYTES = 16 * 1024


async def _cancel_lifespan_task(task: asyncio.Task[None] | None, *, name: str) -> None:
    """Cancel and observe one lifespan task without aborting sibling cleanup.

    A background task can fail before shutdown starts.  Awaiting that failed
    task directly from the lifespan ``finally`` block would skip cancellation
    of later tasks and the infrastructure cleanup call.  Shutdown is a
    best-effort boundary: preserve the primary lifecycle exception while
    observing secondary task failures and continuing cleanup.
    """

    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception as exc:  # broad: shutdown cleanup must not mask lifespan completion
        _LOGGER.warning("Lifespan task failed during shutdown (%s)", type(exc).__name__)
        _LOGGER.exception("BHM lifespan task failed during shutdown: %s", name)


def _rest_usage_operation(request: Request) -> str:
    route = request.scope.get("route")
    route_template = getattr(route, "path", None)
    return normalize_operation(
        f"{request.method.upper()} {route_template or request.url.path}",
        fallback=f"{request.method.upper()} other",
    )


def _rest_usage_status(status_code: int) -> str:
    if 200 <= status_code < 300:
        return "2xx"
    if 300 <= status_code < 400:
        return "3xx"
    if 400 <= status_code < 500:
        return "4xx"
    if 500 <= status_code < 600:
        return "5xx"
    return "error"


def _response_size_from_headers(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw_size = headers.get("content-length")
    if raw_size is None:
        return None
    try:
        return max(int(raw_size), 0)
    except (TypeError, ValueError):
        return None


def _initialize_authoritative_memory_service() -> None:
    rt._memory_service().repository.initialize()


def _boot_report_is_pending() -> bool:
    return app_core_hook._read_boot_report().get("status") == "pending"


async def _finalize_pending_boot_report(warmup_task: asyncio.Task[None]) -> None:
    pending = app_core_hook._read_boot_report()
    if pending.get("status") != "pending":
        return

    try:
        trigger_time = float(pending.get("trigger_time") or time.time())
    except (TypeError, ValueError):
        trigger_time = time.time()

    await warmup_task
    await _wait_for_qdrant_ready()
    _write_json_atomic(
        app_core_hook._BOOT_REPORT_PATH,
        {
            "status": "SUCCESS",
            "elapsed_seconds": round(max(time.time() - trigger_time, 0.0), 2),
            "qdrant": "OK",
            "lm_studio": "READY",
            "timestamp": rt._utc_now_iso(),
        },
    )


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return f'"{pid}"' in result.stdout or f",{pid}," in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _bounded_process_termination_grace_seconds(value: Any) -> int:
    try:
        requested = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("termination grace must be numeric") from exc
    if not math.isfinite(requested):
        raise ValueError("termination grace must be finite")
    return max(min(int(requested), PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS), 1)


async def _terminate_process_tree(
    pid: int,
    *,
    grace_seconds: int = PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS,
    dry_run: bool = False,
) -> dict[str, Any]:
    grace_seconds = _bounded_process_termination_grace_seconds(grace_seconds)
    result: dict[str, Any] = {"pid": pid, "dry_run": dry_run, "terminated": False, "forced": False}
    if pid <= 0:
        result["error"] = "invalid_pid"
        return result
    if dry_run:
        result["running"] = _is_pid_running(pid)
        return result
    if os.name == "nt":
        try:
            soft = await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(pid), "/T"],
                capture_output=True,
                text=True,
                timeout=max(grace_seconds, 1),
                check=False,
            )
            result["soft_exit_code"] = soft.returncode
            result["soft_output"] = (soft.stdout or soft.stderr or "").strip()[:500]
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["soft_error"] = _safe_exception_text(exc)
        await asyncio.sleep(max(grace_seconds, 0))
        if _is_pid_running(pid):
            try:
                forced = await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=max(grace_seconds, 1),
                    check=False,
                )
                result["forced"] = True
                result["force_exit_code"] = forced.returncode
                result["force_output"] = (forced.stdout or forced.stderr or "").strip()[:500]
            except (OSError, subprocess.TimeoutExpired) as exc:
                result["force_error"] = _safe_exception_text(exc)
    else:
        try:
            os.kill(pid, 15)
            await asyncio.sleep(max(grace_seconds, 0))
            if _is_pid_running(pid):
                os.kill(pid, 9)
                result["forced"] = True
        except OSError as exc:
            result["error"] = _safe_exception_text(exc)
    result["terminated"] = not _is_pid_running(pid)
    return result


async def _cleanup_registered_infra_processes(reason: str = "cleanup", dry_run: bool = False) -> dict[str, Any]:
    with app_core_hook._INFRA_SPAWNED_PIDS_LOCK:
        pids = sorted(app_core_hook._INFRA_SPAWNED_PIDS)
    items = []
    for pid in pids:
        kill_result = await _terminate_process_tree(
            pid,
            grace_seconds=PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS,
            dry_run=dry_run,
        )
        items.append({"reason": reason, **kill_result})
    if not dry_run:
        with app_core_hook._INFRA_SPAWNED_PIDS_LOCK:
            app_core_hook._INFRA_SPAWNED_PIDS.difference_update(pid for pid in pids if not _is_pid_running(pid))
    return {"reason": reason, "count": len(items), "items": items}

