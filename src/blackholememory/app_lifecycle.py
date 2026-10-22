"""Domain module app_lifecycle: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import time
import asyncio
import ctypes
import importlib.util
import json
import logging
import math
import os
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from fastapi import HTTPException
from fastapi import Request
from . import app_core_fallback
from . import app_core_hook
from . import app_core
from . import app_hooks
from . import app_ops_task
from . import app_runtime as rt
from .app_runtime import _MAX_CONCURRENT_WRITES, _env_float, _safe_exception_text, _write_json_atomic
from .api_models import MemoryUpsertRequest, MemoryCrystallizeRequest, MemoryMergeRequest, MemoryDetectRequest, BhmHookRequest, BhmHookCompactRequest, BhmHookIdleRequest
from .local_endpoint_policy import open_local_url
from .resource_limits import PROCESS_EXECUTION_LONG_VALIDATOR_TIMEOUT_SECONDS
from .resource_limits import PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS
from .resource_limits import PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS
from .resource_limits import QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS
from .mem0_adapter import StorageNotReady
from .mem0_adapter import ensure_decay_metadata
from .mem0_adapter import get_qdrant_client
from .mem0_adapter import global_collection_name
from .mem0_adapter import memory_decay_score
from .mem0_adapter import storage_runtime_state
from .mem0_adapter import _remote_qdrant_available
from .mcp_surfaces import resolve_mcp_surface
from .context_tier_lifecycle import build_context_tier_lifecycle_receipt
from .hook_queue import HookJobLeaseLost
from .hook_queue import HookJobQueue
from .hook_queue import HookQueueError
from .observation_contract import ObservationIngressV1
from .observation_contract import build_observation_record
from .observation_security import redact_secret_text
from .usage_telemetry import normalize_operation


_LOGGER = logging.getLogger(__name__)


_HOOK_QUEUE_POLL_SECONDS = _env_float("BHM_HOOK_QUEUE_POLL_SECONDS", 0.05, 0.01)


_HOOK_QUEUE_RETRY_BASE_SECONDS = _env_float("BHM_HOOK_QUEUE_RETRY_BASE_SECONDS", 1.0, 0.0)


_HOOK_QUEUE_BOOT_ID = f"bhm-hook-worker-{uuid.uuid4().hex[:12]}"


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


def _hook_job_result_summary(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"success": True, "resultType": type(result).__name__}
    summary: dict[str, Any] = {
        "success": bool(result.get("success", True)),
        "action": str(result.get("action") or "completed")[:200],
    }
    if result.get("reason"):
        summary["reason"] = redact_secret_text(str(result["reason"])).value[:1000]
    observation = result.get("observation")
    if isinstance(observation, dict):
        summary["observation"] = {"id": str(observation.get("id") or observation.get("eventId") or "")}
    memory = result.get("memory")
    if isinstance(memory, dict):
        summary["memory"] = {"id": str(memory.get("id") or memory.get("source_id") or "")}
    if isinstance(result.get("source_ids"), list):
        summary["sourceCount"] = len(result["source_ids"])
    tier_lifecycle = result.get("tier_lifecycle")
    if isinstance(tier_lifecycle, dict):
        summary["tierLifecycle"] = {
            "receiptId": str(tier_lifecycle.get("receipt_id") or ""),
            "phase": str(tier_lifecycle.get("phase") or "unmapped"),
            "effectClass": str(tier_lifecycle.get("effect_class") or "none"),
            "promotion": str((tier_lifecycle.get("promotion") or {}).get("action") or "none"),
        }
    steps = result.get("steps")
    if isinstance(steps, dict):
        summary["steps"] = {
            str(name): {
                "success": bool(value.get("success", True)) if isinstance(value, dict) else True,
                "status": str(value.get("status") or "completed") if isinstance(value, dict) else "completed",
            }
            for name, value in steps.items()
        }
    return summary


def _hook_job_log_context(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    return {
        "job_id": str(job.get("jobId") or ""),
        "event_id": str(job.get("eventId") or payload.get("eventId") or ""),
        "correlation_id": str(payload.get("correlationId") or payload.get("sessionId") or ""),
        "kind": str(job.get("kind") or ""),
        "attempts": int(job.get("attempts") or 0),
        "max_attempts": int(job.get("maxAttempts") or 0),
    }


async def _execute_hook_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise HookQueueError("hook job payload missing")
    kind = str(job.get("kind") or "")
    if kind == "compact":
        request = BhmHookCompactRequest.model_validate(payload)
        return await asyncio.to_thread(_handle_compact_hook, request)
    if kind == "idle":
        request = BhmHookIdleRequest.model_validate(payload)
        return await _run_idle_reflection_pipeline(request)
    raise HookQueueError(f"unsupported hook job kind: {kind}")


async def _hook_job_heartbeat(queue: HookJobQueue, job_id: str, owner: str) -> None:
    interval = max(app_core_hook._HOOK_QUEUE_LEASE_SECONDS / 3.0, 1.0)
    while True:
        await asyncio.sleep(interval)
        renewed = await asyncio.to_thread(
            queue.renew_lease,
            job_id,
            owner=owner,
            lease_seconds=app_core_hook._HOOK_QUEUE_LEASE_SECONDS,
        )
        if not renewed:
            return


async def _hook_queue_worker(*, worker_name: str, kinds: tuple[str, ...], stop_event: asyncio.Event) -> None:
    queue = app_core_hook._hook_queue()
    owner = f"{_HOOK_QUEUE_BOOT_ID}:{worker_name}"
    while True:
        try:
            job = await asyncio.to_thread(
                queue.claim_next,
                kinds=kinds,
                owner=owner,
                lease_seconds=app_core_hook._HOOK_QUEUE_LEASE_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # broad: background worker must survive transient queue failures
            _LOGGER.warning("Hook queue claim failed (%s)", type(exc).__name__)
            if stop_event.is_set():
                return
            await asyncio.sleep(_HOOK_QUEUE_POLL_SECONDS)
            continue
        if job is None:
            if stop_event.is_set():
                return
            await asyncio.sleep(_HOOK_QUEUE_POLL_SECONDS)
            continue

        job_id = str(job["jobId"])
        heartbeat = asyncio.create_task(_hook_job_heartbeat(queue, job_id, owner))
        try:
            result = await _execute_hook_job(job)
            await asyncio.to_thread(
                queue.complete,
                job_id,
                owner=owner,
                result=_hook_job_result_summary(result),
            )
        except asyncio.CancelledError:
            _LOGGER.info(
                "hook_job_cancelled",
                extra={**_hook_job_log_context(job), "classification": "cancelled"},
            )
            raise
        except Exception as exc:  # broad: hook job failure is recorded and worker continues
            redacted_error = _safe_exception_text(exc, limit=4000)
            retry_delay = _HOOK_QUEUE_RETRY_BASE_SECONDS * (2 ** max(int(job.get("attempts") or 1) - 1, 0))
            try:
                status = await asyncio.to_thread(
                    queue.fail,
                    job_id,
                    owner=owner,
                    error=redacted_error,
                    retry_delay_seconds=retry_delay,
                )
                _LOGGER.warning(
                    "hook_job_failed",
                    extra={
                        **_hook_job_log_context(job),
                        "classification": "retryable" if status == "queued" else "terminal",
                        "status": status,
                        "error": redacted_error,
                    },
                )
            except HookJobLeaseLost:
                _LOGGER.error(
                    "hook_job_failure_recording_lease_lost",
                    extra={**_hook_job_log_context(job), "classification": "lease_lost"},
                )
            except Exception as record_exc:  # broad: failure recording is best effort
                _LOGGER.exception(
                    "hook_job_failure_recording_failed",
                    extra={
                        **_hook_job_log_context(job),
                        "classification": "failure_recording",
                        "error": redact_secret_text(str(record_exc)).value[:4000],
                    },
                )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass


async def _start_hook_queue_workers() -> None:
    pass  # globals moved to domain modules
    if any(not task.done() for task in app_hooks._HOOK_QUEUE_TASKS):
        app_hooks._HOOK_QUEUE_ACCEPTING = True
        return
    queue = app_core_hook._hook_queue()
    recovered = await asyncio.to_thread(queue.recover_processing)
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    for app_ops_task.index in range(app_core_hook._HOOK_COMPACT_WORKERS):
        tasks.append(
            asyncio.create_task(
                _hook_queue_worker(
                    worker_name=f"compact-{app_ops_task.index + 1}",
                    kinds=("compact",),
                    stop_event=stop_event,
                ),
                name=f"bhm-hook-compact-{app_ops_task.index + 1}",
            )
        )
    for app_ops_task.index in range(app_core_hook._HOOK_IDLE_WORKERS):
        tasks.append(
            asyncio.create_task(
                _hook_queue_worker(
                    worker_name=f"idle-{app_ops_task.index + 1}",
                    kinds=("idle",),
                    stop_event=stop_event,
                ),
                name=f"bhm-hook-idle-{app_ops_task.index + 1}",
            )
        )
    app_core_hook._HOOK_QUEUE_STOP_EVENT = stop_event
    app_hooks._HOOK_QUEUE_TASKS = tasks
    app_hooks._HOOK_QUEUE_ACCEPTING = True
    print(
        f"[INFO] BHM hook queue ready: capacity={queue.capacity} "
        f"compact_workers={app_core_hook._HOOK_COMPACT_WORKERS} idle_workers={app_core_hook._HOOK_IDLE_WORKERS} recovered={recovered}",
        flush=True,
    )


async def _stop_hook_queue_workers() -> None:
    pass  # globals moved to domain modules
    app_hooks._HOOK_QUEUE_ACCEPTING = False
    stop_event = app_core_hook._HOOK_QUEUE_STOP_EVENT
    tasks = list(app_hooks._HOOK_QUEUE_TASKS)
    if stop_event is not None:
        stop_event.set()
    if tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=app_core_hook._HOOK_QUEUE_DRAIN_SECONDS,
            )
        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            print(
                f"[WARN] Hook queue drain exceeded {app_core_hook._HOOK_QUEUE_DRAIN_SECONDS:.1f}s; "
                "leased jobs will be recovered on restart.",
                flush=True,
            )
    app_hooks._HOOK_QUEUE_TASKS = []
    app_core_hook._HOOK_QUEUE_STOP_EVENT = None


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


_HOOK_TRANSIT_ITEM_CHAR_LIMIT = 4000


_HOOK_SCRIPT_MODULE_CACHE: dict[str, Any] = {}


_HOOK_SCRIPT_MODULE_LOCK = threading.RLock()


def _hook_timestamp(request: BhmHookRequest) -> str:
    return request.timestamp or rt._utc_now_iso()


def _append_hook_observation(request: BhmHookRequest, endpoint: str) -> dict:
    ingress = ObservationIngressV1(
        schemaVersion=request.schemaVersion,
        eventId=request.eventId,
        hookType=request.hookType,
        sessionId=request.sessionId,
        correlationId=request.correlationId,
        parentEventId=request.parentEventId,
        project=request.project,
        cwd=request.cwd,
        timestamp=_hook_timestamp(request),
        source=request.source,
        endpoint=endpoint,
        payloadState=request.payloadState,
        sensitivity=request.sensitivity,
        data=dict(request.data or {}),
        metadata=dict(request.metadata or {}),
    )
    item = build_observation_record(ingress)
    app_core_hook._append_observation(item)
    return item


def _hook_text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _hook_item_text(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, str):
        text = item.strip()
    else:
        try:
            text = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).strip()
        except TypeError:
            text = str(item).strip()
    return text[:_HOOK_TRANSIT_ITEM_CHAR_LIMIT]


def _with_context_tier_lifecycle_receipt(request: BhmHookRequest) -> tuple[BhmHookRequest, dict[str, Any]]:
    """Bind a content-free tier lifecycle receipt to the sanitized observation.

    This is intentionally metadata-only.  It creates no promotion lock and
    does not alter the legacy compact/idle behaviour beyond persisting an
    auditable lifecycle anchor with the existing observation event.
    """

    source_ids = app_core_hook._lifecycle_source_ids(request)
    receipt = build_context_tier_lifecycle_receipt(
        project=request.project,
        session_id=request.sessionId,
        event_id=request.eventId,
        hook_type=request.hookType,
        parent_event_id=request.parentEventId,
        source_ids=source_ids,
    )
    metadata = dict(request.metadata or {})
    metadata["context_tier_lifecycle"] = receipt
    return request.model_copy(update={"metadata": metadata}), receipt


def _hook_compact_concepts(request: BhmHookCompactRequest) -> list[str]:
    concepts = [
        "bhm",
        "codex-hook",
        "pre-compact",
        "reflex",
        "crystallization",
    ]
    concepts.extend(app_core_hook._coerce_hook_text_list(request.concepts))
    concepts.extend(app_core_hook._coerce_hook_text_list(app_core_hook._hook_data_value(request, "concepts", "tags")))
    return app_core_hook._dedupe_text(concepts)


def _hook_compact_files(request: BhmHookCompactRequest) -> list[str]:
    files = app_core_hook._coerce_hook_text_list(request.files)
    files.extend(app_core_hook._coerce_hook_text_list(app_core_hook._hook_data_value(request, "files", "files_touched", "filesTouched")))
    return app_core_hook._dedupe_text(files)


def _materialize_hook_transit_sources(request: BhmHookCompactRequest, transit_items: list[Any]) -> list[dict]:
    records: list[dict] = []
    concepts = _hook_compact_concepts(request)
    files = _hook_compact_files(request)
    for app_ops_task.index, item in enumerate(transit_items[:app_core_hook._HOOK_TRANSIT_SOURCE_LIMIT]):
        content = _hook_item_text(item)
        if not content:
            continue
        record_content = "\n".join(
            [
                "BHM pre-compact transit buffer:",
                f"hookType: {request.hookType}",
                f"sessionId: {request.sessionId}",
                f"project: {request.project}",
                f"cwd: {request.cwd}",
                f"index: {app_ops_task.index}",
                "payload:",
                content,
            ]
        )
        _, record = app_core._upsert_live_memory(
            MemoryUpsertRequest(
                upsert_key=f"hook-compact-source:{request.project}:{request.sessionId}:{app_ops_task.index}",
                project=request.project,
                type="transient-context",
                content=record_content,
                concepts=concepts,
                files=files,
            )
        )
        records.append(record)
    return records


def _build_hook_crystallize_request(request: BhmHookCompactRequest, source_ids: list[str]) -> MemoryCrystallizeRequest:
    title = (
        _hook_text_or_none(request.title)
        or _hook_text_or_none(app_core_hook._hook_data_value(request, "title"))
        or f"{request.project} pre-compact reflex crystal"
    )
    summary = (
        _hook_text_or_none(request.summary)
        or _hook_text_or_none(app_core_hook._hook_data_value(request, "summary", "reason"))
        or f"Emergency Codex pre-compact reflex captured {len(source_ids)} source item(s)."
    )
    upsert_key = (
        _hook_text_or_none(request.upsert_key)
        or _hook_text_or_none(app_core_hook._hook_data_value(request, "upsert_key", "upsertKey"))
        or f"hook-compact-crystal:{request.project}:{request.sessionId}"
    )
    return MemoryCrystallizeRequest(
        source_ids=source_ids,
        project=request.project,
        title=title,
        summary=summary,
        target_type=request.target_type,
        concepts=_hook_compact_concepts(request),
        files=_hook_compact_files(request),
        upsert_key=upsert_key,
    )


def _handle_compact_hook(request: BhmHookCompactRequest) -> dict:
    request = app_core_hook._ensure_hook_request_identity(request)
    request, tier_lifecycle = _with_context_tier_lifecycle_receipt(request)
    observation = _append_hook_observation(request, "compact")
    transit_items = app_core_hook._hook_transit_items(request)
    source_ids = app_core_hook._extract_hook_source_ids(request, transit_items)
    materialized_sources: list[dict] = []

    if not source_ids:
        materialized_sources = _materialize_hook_transit_sources(request, transit_items)
        source_ids = app_core_hook._dedupe_text([str(record.get("source_id") or "") for record in materialized_sources])

    if not source_ids:
        return {
            "success": True,
            "action": "skipped",
            "reason": "empty_transit_buffer",
            "hook": {"type": request.hookType, "sessionId": request.sessionId, "project": request.project},
            "observation": {"id": observation.get("id")},
            "tier_lifecycle": tier_lifecycle,
        }

    action, record = app_core._crystallize_memories(_build_hook_crystallize_request(request, source_ids))
    return {
        "success": True,
        "action": action,
        "hook": {"type": request.hookType, "sessionId": request.sessionId, "project": request.project},
        "source_ids": source_ids,
        "materialized_source_ids": [record.get("source_id") for record in materialized_sources],
        "memory": app_core._serialize_memory_record(record),
        "observation": {"id": observation.get("id")},
        "tier_lifecycle": tier_lifecycle,
    }


def _apply_idle_duplicate_merge(request: BhmHookIdleRequest) -> dict:
    candidates = app_core._detect_duplicates(
        MemoryDetectRequest(project=request.project, limit=request.duplicate_limit, include_archived=False)
    )
    merged: list[dict[str, Any]] = []
    for candidate in candidates:
        if float(candidate.get("score") or 0.0) < 0.92:
            continue
        try:
            result = _merge_memories(
                MemoryMergeRequest(
                    project=request.project,
                    source_id=str(candidate.get("right_id") or ""),
                    target_id=str(candidate.get("left_id") or ""),
                    archive_source=True,
                )
            )
            merged.append(
                {
                    "source_id": (result.get("source") or {}).get("source_id"),
                    "target_id": (result.get("target") or {}).get("source_id"),
                    "score": candidate.get("score"),
                }
            )
        except Exception as exc:  # broad: merge item errors are returned alongside successful items
            merged.append({"error": _safe_exception_text(exc), "candidate": candidate})
    return {"candidates": len(candidates), "merged": merged}


def _refresh_decay_scores_for_project(project: str, limit: int) -> dict:
    client = get_qdrant_client()
    now_dt = datetime.now(timezone.utc)
    recalculated_at = now_dt.isoformat().replace("+00:00", "Z")
    updated = 0
    scanned = 0
    errors: list[dict[str, str]] = []
    route = rt._project_projection_route(project)
    collection_names = [global_collection_name()]
    if route.collection_name is not None:
        collection_names.insert(0, route.collection_name)

    for collection_name in collection_names:
        offset = None
        while updated < limit:
            try:
                points, offset = client.scroll(
                    collection_name=collection_name,
                    limit=min(128, limit - updated),
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception as exc:  # broad: collection scan records error and continues
                errors.append({"collection": collection_name, "error": _safe_exception_text(exc)})
                break
            if not points:
                break
            scanned += len(points)
            for point in points:
                payload = dict(getattr(point, "payload", None) or {})
                normalized = ensure_decay_metadata(
                    payload,
                    fallback_at=str(payload.get("updated_at") or payload.get("created_at") or recalculated_at),
                )
                raw_score = float(payload.get("score") or payload.get("raw_qdrant_score") or 1.0)
                decay_score = memory_decay_score(normalized, raw_qdrant_score=raw_score, now=now_dt)
                try:
                    client.set_payload(
                        collection_name=collection_name,
                        payload={
                            "importance_score": normalized["importance_score"],
                            "access_count": normalized["access_count"],
                            "last_accessed_at": normalized["last_accessed_at"],
                            "decay_score": decay_score,
                            "decay_recalculated_at": recalculated_at,
                        },
                        points=[getattr(point, "id")],
                    )
                    updated += 1
                except Exception as exc:  # broad: point update records error and continues
                    errors.append({"collection": collection_name, "point_id": str(getattr(point, "id", "")), "error": _safe_exception_text(exc)})
            if offset is None:
                break
        if updated >= limit:
            break
    return {"scanned": scanned, "updated": updated, "errors": errors}


def _repo_script_module(file_name: str, module_name: str) -> Any | None:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / file_name
    if not script_path.exists():
        return None
    with _HOOK_SCRIPT_MODULE_LOCK:
        cached = _HOOK_SCRIPT_MODULE_CACHE.get(file_name)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(module_name, script_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _HOOK_SCRIPT_MODULE_CACHE[file_name] = module
        return module


async def _run_idle_graph_healer(request: BhmHookIdleRequest) -> dict:
    module = _repo_script_module("bhm_graph_healer.py", "bhm_graph_healer_runtime")
    if module is None:
        return {"status": "skipped", "reason": "module_unavailable"}
    summary = await module.heal_graph(
        dry_run=not request.apply_graph_healer,
        max_orphans=request.max_orphans,
        max_links=request.max_links,
        rate_limit_ms=0,
    )
    return summary.to_dict() if hasattr(summary, "to_dict") else dict(summary)


async def _run_idle_reflection_daemon(request: BhmHookIdleRequest) -> dict:
    module = _repo_script_module("bhm_reflection_daemon.py", "bhm_reflection_daemon_runtime")
    if module is None:
        return {"status": "skipped", "reason": "module_unavailable"}
    return await module.run_reflection_cycle(
        project_name=request.project,
        dry_run=not request.apply_reflection,
        limit=request.reflection_limit,
        scan_limit=request.reflection_scan_limit,
        llm_timeout=float(request.reflection_timeout),
    )


async def _run_idle_reflection_pipeline(request: BhmHookIdleRequest) -> dict:
    request = app_core_hook._ensure_hook_request_identity(request)
    request, tier_lifecycle = _with_context_tier_lifecycle_receipt(request)
    result: dict[str, Any] = {
        "success": True,
        "hook": {"type": request.hookType, "sessionId": request.sessionId, "project": request.project},
        "tier_lifecycle": tier_lifecycle,
        "started_at": rt._utc_now_iso(),
        "steps": {},
    }
    for step_name, step_call in (
        ("observe", lambda: _append_hook_observation(request, "idle")),
        ("duplicate_merge", lambda: _apply_idle_duplicate_merge(request)),
        ("decay_refresh", lambda: _refresh_decay_scores_for_project(request.project, request.decay_limit)),
    ):
        try:
            result["steps"][step_name] = await asyncio.to_thread(step_call)
        except Exception as exc:  # broad: maintenance step failures are captured in result
            result["steps"][step_name] = {"success": False, "error": _safe_exception_text(exc)}

    for step_name, step_call in (
        ("graph_healer", lambda: _run_idle_graph_healer(request)),
        ("reflection_daemon", lambda: _run_idle_reflection_daemon(request)),
    ):
        try:
            result["steps"][step_name] = await step_call()
        except Exception as exc:  # broad: maintenance step failures are captured in result
            result["steps"][step_name] = {"success": False, "error": _safe_exception_text(exc)}

    result["finished_at"] = rt._utc_now_iso()
    return result


def _merge_memories(request: MemoryMergeRequest) -> dict:
    if request.source_id == request.target_id:
        raise HTTPException(status_code=400, detail="source_id and target_id must differ")
    source = rt._find_live_memory(request.source_id, request.project)
    target = rt._find_live_memory(request.target_id, request.project)
    if source is None:
        raise HTTPException(status_code=404, detail="source memory not found")
    if target is None:
        raise HTTPException(status_code=404, detail="target memory not found")
    target, source = app_core._merge_memory_records(
        source,
        target,
        archive_source=request.archive_source,
        target_id=request.target_id,
    )
    app_core._save_live_memories([target, source])
    return {"target": target, "source": source, "archived_source": request.archive_source}
