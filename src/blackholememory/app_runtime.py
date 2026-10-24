"""Runtime core: storage accessors, read/write backpressure and provider warmup.

Moved out of app.py (FIX-017 S2). Names tests patch, and mutable state, are reached by callers through
``from . import app_runtime as rt`` (late binding) so monkeypatching this module takes effect.
"""

from __future__ import annotations

import time
import asyncio
import json
import os
import re
import threading
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from .config import settings
from .local_endpoint_policy import open_local_url
from .local_endpoint_policy import read_bounded_response
from .memory_pulse_bus import MemoryPulseBus
from .resource_limits import LLM_HTTP_TIMEOUT_SECONDS
from .filesystem_boundaries import replace_bytes_safely
from .mem0_adapter import StorageNotReady
from .mem0_adapter import get_global_core_memory
from .mem0_adapter import get_project_mem0_memory
from .mem0_adapter import get_qdrant_client
from .mem0_adapter import global_collection_name
from .mem0_adapter import local_collection_name
from .project_registry import get_default_project_registry
from .runtime_storage import MemoryStoreMode
from .runtime_storage import runtime_storage_state as memory_runtime_storage_state
from .runtime_storage import resolve_runtime_storage_config
from .projection_generation_binding import resolve_projection_route
from .memory_service import MemoryServiceNotReady
from .memory_service import SQLiteMemoryService
from .observation_security import redact_secret_text


# Exception text crosses several REST/MCP and operator-facing boundaries in
# this module.  Keep the diagnostic value bounded while applying the same
# secret/path redaction policy used by observation ingress.  This helper is
# intentionally local to the app boundary: internal exceptions retain their
# original traceback for logs, but serialized responses never do.
_PUBLIC_EXCEPTION_TEXT_LIMIT = 500


def _safe_exception_text(
    exc: BaseException,
    *,
    limit: int = _PUBLIC_EXCEPTION_TEXT_LIMIT,
    fallback: str = "operation failed",
) -> str:
    bounded_limit = max(int(limit), 1)
    value = redact_secret_text(str(exc)).value.strip()
    return (value[:bounded_limit] or fallback)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(int(os.getenv(name, str(default))), minimum)
    except (TypeError, ValueError):
        return max(default, minimum)


def _env_float(name: str, default: float, minimum: float = 0.0, maximum: float | None = None) -> float:
    upper = max(float(maximum), minimum) if maximum is not None else None
    try:
        value = max(float(os.getenv(name, str(default))), minimum)
    except (TypeError, ValueError):
        value = max(default, minimum)
    return min(value, upper) if upper is not None else value


_MAX_CONCURRENT_WRITES = _env_int("BHM_MAX_CONCURRENT_WRITES", 10, 1)


_WRITE_QUEUE_LIMIT = _env_int("BHM_WRITE_QUEUE_LIMIT", 20, 0)


_WRITE_RETRY_AFTER_SECONDS = _env_int("BHM_WRITE_RETRY_AFTER_SECONDS", 5, 1)


_WRITE_ACQUIRE_TIMEOUT_SECONDS = _env_float("BHM_WRITE_ACQUIRE_TIMEOUT_SECONDS", 1.0, 0.0)


_WRITE_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_WRITES)


_WRITE_BACKPRESSURE_LOCK = asyncio.Lock()


_WRITE_BACKPRESSURE_ACTIVE = 0


_WRITE_BACKPRESSURE_WAITING = 0


_MAX_CONCURRENT_READS = _env_int("BHM_MAX_CONCURRENT_READS", 10, 1)


_READ_QUEUE_LIMIT = _env_int("BHM_READ_QUEUE_LIMIT", 20, 0)


_READ_RETRY_AFTER_SECONDS = _env_int("BHM_READ_RETRY_AFTER_SECONDS", 1, 1)


_READ_ACQUIRE_TIMEOUT_SECONDS = _env_float("BHM_READ_ACQUIRE_TIMEOUT_SECONDS", 1.0, 0.0)


_READ_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_READS)


_READ_BACKPRESSURE_LOCK = asyncio.Lock()


_READ_BACKPRESSURE_ACTIVE = 0


_READ_BACKPRESSURE_WAITING = 0


class ResponseTimeout(Exception):
    """Local transport/readiness timeout that should fall back to disk snapshots."""


_PROVIDER_WARMUP_REQUIRED = os.getenv("BHM_PROVIDER_WARMUP_DISABLED", "").lower() not in {"1", "true", "yes"}


_PROVIDER_WARMUP_TIMEOUT_SECONDS = _env_float(
    "BHM_PROVIDER_WARMUP_TIMEOUT_SECONDS",
    5.0,
    0.1,
    LLM_HTTP_TIMEOUT_SECONDS,
)


_PROVIDER_WARMUP_INITIAL_DELAY_SECONDS = _env_float("BHM_PROVIDER_WARMUP_INITIAL_DELAY_SECONDS", 1.0, 0.1)


_PROVIDER_WARMUP_MAX_DELAY_SECONDS = _env_float("BHM_PROVIDER_WARMUP_MAX_DELAY_SECONDS", 30.0, 1.0)


_PROVIDER_READINESS_WAIT_SECONDS = _env_float("BHM_PROVIDER_READINESS_WAIT_SECONDS", 5.0, 0.0)


_PROVIDER_WARMUP_MAX_RESPONSE_BYTES = min(
    _env_int("BHM_PROVIDER_WARMUP_MAX_RESPONSE_BYTES", 16 * 1024, 512),
    256 * 1024,
)


_PROVIDER_CHAT_WARMUP_ENABLED = os.getenv("BHM_PROVIDER_CHAT_WARMUP", "").lower() in {
    "1",
    "true",
    "yes",
}


_PROVIDER_EMBEDDING_WARMUP_TIMEOUT_SECONDS = _env_float(
    "BHM_PROVIDER_EMBEDDING_WARMUP_TIMEOUT_SECONDS",
    _PROVIDER_WARMUP_TIMEOUT_SECONDS,
    0.1,
    LLM_HTTP_TIMEOUT_SECONDS,
)


_PROVIDER_EMBEDDING_WARMUP_MAX_RESPONSE_BYTES = min(
    _env_int("BHM_PROVIDER_EMBEDDING_WARMUP_MAX_RESPONSE_BYTES", 256 * 1024, 1024),
    256 * 1024,
)


_PROVIDER_MEMORY_WARMUP_ENABLED = os.getenv("BHM_PROVIDER_MEMORY_WARMUP", "").lower() in {
    "1",
    "true",
    "yes",
}


_PROVIDER_MEMORY_WARMUP_MAX_PROJECTS = _env_int("BHM_PROVIDER_MEMORY_WARMUP_MAX_PROJECTS", 8, 1)


_PROVIDER_WARMUP_READY = threading.Event()


_PROVIDER_WARMUP_STATUS_LOCK = threading.RLock()


_PROVIDER_WARMUP_STATUS: dict[str, Any] = {
    "enabled": _PROVIDER_WARMUP_REQUIRED,
    "ready": not _PROVIDER_WARMUP_REQUIRED,
    "attempts": 0,
    "last_error": "",
    "updated_at": "",
    # Semantic retrieval depends on the embedding endpoint, never on the
    # operator's currently selected chat model. Keep that distinction in the
    # status contract so a busy/changed coding model cannot falsely breach the
    # memory SLO.
    "probe_kind": "embedding",
    "embedding_warmup_enabled": _PROVIDER_WARMUP_REQUIRED,
    "embedding_ready": not _PROVIDER_WARMUP_REQUIRED,
    "embedding_attempts": 0,
    "embedding_last_error": "",
    "embedding_phase": "disabled" if not _PROVIDER_WARMUP_REQUIRED else "pending",
    "chat_warmup_enabled": _PROVIDER_CHAT_WARMUP_ENABLED,
    "chat_ready": not _PROVIDER_CHAT_WARMUP_ENABLED,
    "chat_attempts": 0,
    "chat_last_error": "",
    "chat_phase": "disabled" if not _PROVIDER_CHAT_WARMUP_ENABLED else "pending",
    "memory_warmup_enabled": _PROVIDER_MEMORY_WARMUP_ENABLED,
    "memory_ready": not _PROVIDER_MEMORY_WARMUP_ENABLED,
    "memory_projects": [],
    "memory_skipped_projects": [],
    "memory_last_error": "",
    "memory_phase": "disabled" if not _PROVIDER_MEMORY_WARMUP_ENABLED else "pending",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _set_provider_warmup_status(**patch: Any) -> None:
    with _PROVIDER_WARMUP_STATUS_LOCK:
        _PROVIDER_WARMUP_STATUS.update(patch)
        _PROVIDER_WARMUP_STATUS["updated_at"] = _utc_now_iso()


def _get_provider_warmup_status() -> dict[str, Any]:
    with _PROVIDER_WARMUP_STATUS_LOCK:
        status = dict(_PROVIDER_WARMUP_STATUS)
    status["ready"] = _PROVIDER_WARMUP_READY.is_set()
    return status


def _provider_warmup_url() -> str:
    """Return the embedding endpoint used by the required memory probe."""

    explicit = os.getenv("BHM_PROVIDER_EMBEDDING_WARMUP_URL", "").strip()
    if explicit:
        return explicit
    return f"{settings.mem0_openai_base_url.rstrip('/')}/embeddings"


def _post_provider_warmup_probe() -> None:
    """Verify the provider BHM retrieval actually needs, without retaining a vector."""

    payload = {
        "model": settings.mem0_embedding_model,
        "input": ["bhm provider readiness"],
        "encoding_format": "float",
    }
    headers = {"Content-Type": "application/json"}
    if settings.mem0_api_key:
        headers["Authorization"] = f"Bearer {settings.mem0_api_key}"
    request = urllib.request.Request(
        _provider_warmup_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with open_local_url(request, timeout=_PROVIDER_EMBEDDING_WARMUP_TIMEOUT_SECONDS) as response:
        raw = read_bounded_response(response, limit=_PROVIDER_EMBEDDING_WARMUP_MAX_RESPONSE_BYTES)
    try:
        response_payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("provider warmup response is not valid JSON") from exc
    data = response_payload.get("data") if isinstance(response_payload, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise OSError("provider warmup response does not contain embedding data")


def _post_provider_embedding_warmup_probe() -> None:
    """Load the configured embedding model without returning or persisting vectors.

    This is deliberately an opt-in startup probe, enabled only by the explicit
    semantic-fusion launcher switch.  A tiny local request removes the model's
    first-use latency from the first operator-requested semantic query while
    preserving SQLite authority and the projection-only boundary.
    """
    payload = {
        "model": settings.mem0_embedding_model,
        "input": ["bhm semantic fusion warmup"],
        "encoding_format": "float",
    }
    headers = {"Content-Type": "application/json"}
    if settings.mem0_api_key:
        headers["Authorization"] = f"Bearer {settings.mem0_api_key}"
    request = urllib.request.Request(
        f"{settings.mem0_openai_base_url.rstrip('/')}/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with open_local_url(request, timeout=_PROVIDER_EMBEDDING_WARMUP_TIMEOUT_SECONDS) as response:
        raw = read_bounded_response(response, limit=_PROVIDER_EMBEDDING_WARMUP_MAX_RESPONSE_BYTES)
    try:
        response_payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("provider embedding warmup response is not valid JSON") from exc
    data = response_payload.get("data") if isinstance(response_payload, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise OSError("provider embedding warmup response does not contain embedding data")


def _post_provider_chat_warmup_probe() -> None:
    """Optionally verify a chat model without making it a memory health gate."""

    payload = {
        "model": settings.mem0_llm_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    disable_thinking = os.getenv("BHM_PROVIDER_DISABLE_THINKING", "").strip().lower()
    if disable_thinking in {"1", "true", "yes", "on"} or "qwen" in settings.mem0_llm_model.lower():
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Content-Type": "application/json"}
    if settings.mem0_api_key:
        headers["Authorization"] = f"Bearer {settings.mem0_api_key}"
    request = urllib.request.Request(
        f"{settings.mem0_openai_base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with open_local_url(request, timeout=_PROVIDER_WARMUP_TIMEOUT_SECONDS) as response:
        raw = read_bounded_response(response, limit=_PROVIDER_WARMUP_MAX_RESPONSE_BYTES)
    try:
        response_payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("provider chat warmup response is not valid JSON") from exc
    choices = response_payload.get("choices") if isinstance(response_payload, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise OSError("provider chat warmup response does not contain a completion choice")


def _provider_memory_warmup_projects() -> list[str]:
    raw = os.getenv("BHM_PROVIDER_WARMUP_PROJECTS", "blackholememory")
    projects: list[str] = []
    for value in raw.split(","):
        project = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
        if project and project not in projects:
            projects.append(project)
    return projects[:_PROVIDER_MEMORY_WARMUP_MAX_PROJECTS]


def _post_provider_memory_warmup_probe() -> tuple[list[str], list[str]]:
    """Instantiate existing Mem0/Qdrant handles without creating collections or vectors.

    Only already-existing collections are warmed.  This keeps the probe
    projection/read-only: a missing project collection is reported as skipped,
    never created during startup.
    """
    client = get_qdrant_client()
    warmed: list[str] = []
    skipped: list[str] = []
    for project in _provider_memory_warmup_projects():
        route = _project_projection_route(project)
        collection_name = route.collection_name
        if collection_name is None:
            skipped.append(project)
            continue
        if not client.collection_exists(collection_name):
            skipped.append(project)
            continue
        memory = get_project_mem0_memory(
            project,
            collection_name=collection_name,
            ensure_collection=False,
        )
        _ = memory.embedding_model
        warmed.append(project)

    global_name = global_collection_name()
    if client.collection_exists(global_name):
        _ = get_global_core_memory().embedding_model
    return warmed, skipped


async def warmup_provider_probe() -> None:
    if not _PROVIDER_WARMUP_REQUIRED:
        return

    delay = _PROVIDER_WARMUP_INITIAL_DELAY_SECONDS
    attempts = 0
    while True:
        attempts += 1
        _set_provider_warmup_status(
            attempts=attempts,
            last_error="",
            phase="probing",
            probe_kind="embedding",
            embedding_warmup_enabled=True,
            chat_warmup_enabled=_PROVIDER_CHAT_WARMUP_ENABLED,
        )
        try:
            await run_in_threadpool(_post_provider_warmup_probe)
            _set_provider_warmup_status(
                embedding_ready=True,
                embedding_attempts=attempts,
                embedding_last_error="",
                embedding_phase="ready",
            )
            if _PROVIDER_MEMORY_WARMUP_ENABLED:
                _set_provider_warmup_status(memory_phase="probing", memory_last_error="")
                try:
                    warmed, skipped = await run_in_threadpool(_post_provider_memory_warmup_probe)
                    _set_provider_warmup_status(
                        memory_ready=True,
                        memory_projects=warmed,
                        memory_skipped_projects=skipped,
                        memory_phase="ready" if warmed else "degraded",
                    )
                except asyncio.CancelledError:
                    raise
                except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, OSError, RuntimeError) as exc:
                    _set_provider_warmup_status(
                        memory_ready=False,
                        memory_last_error=_safe_exception_text(exc),
                        memory_phase="degraded",
                    )
            _PROVIDER_WARMUP_READY.set()
            _set_provider_warmup_status(ready=True, phase="ready", last_error="")
            if _PROVIDER_CHAT_WARMUP_ENABLED:
                _set_provider_warmup_status(chat_attempts=1, chat_last_error="", chat_phase="probing")
                try:
                    await run_in_threadpool(_post_provider_chat_warmup_probe)
                    _set_provider_warmup_status(chat_ready=True, chat_last_error="", chat_phase="ready")
                except asyncio.CancelledError:
                    raise
                except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
                    _set_provider_warmup_status(
                        chat_ready=False,
                        chat_last_error=_safe_exception_text(exc),
                        chat_phase="degraded",
                    )
            return
        except asyncio.CancelledError:
            raise
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            _set_provider_warmup_status(ready=False, phase="retrying", last_error=_safe_exception_text(exc))
            await asyncio.sleep(delay)
            delay = min(delay * 2, _PROVIDER_WARMUP_MAX_DELAY_SECONDS)


def _ensure_provider_warmup_ready_sync() -> None:
    if _PROVIDER_WARMUP_READY.is_set():
        return
    if not _PROVIDER_WARMUP_READY.wait(_PROVIDER_READINESS_WAIT_SECONDS):
        raise ResponseTimeout("provider warmup is still in progress")


async def _ensure_provider_warmup_ready() -> None:
    if _PROVIDER_WARMUP_READY.is_set():
        return
    ready = await asyncio.to_thread(_PROVIDER_WARMUP_READY.wait, _PROVIDER_READINESS_WAIT_SECONDS)
    if not ready:
        raise ResponseTimeout("provider warmup is still in progress")


def _write_backpressure_headers() -> dict[str, str]:
    return {"Retry-After": str(_WRITE_RETRY_AFTER_SECONDS)}


@asynccontextmanager
async def _bounded_write(operation: str):
    global _WRITE_BACKPRESSURE_ACTIVE, _WRITE_BACKPRESSURE_WAITING

    async with _WRITE_BACKPRESSURE_LOCK:
        queued_or_running = _WRITE_BACKPRESSURE_ACTIVE + _WRITE_BACKPRESSURE_WAITING
        queue_capacity = _MAX_CONCURRENT_WRITES + _WRITE_QUEUE_LIMIT
        if queued_or_running >= queue_capacity:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "write_backpressure",
                    "operation": operation,
                    "active": _WRITE_BACKPRESSURE_ACTIVE,
                    "waiting": _WRITE_BACKPRESSURE_WAITING,
                    "max_concurrent_writes": _MAX_CONCURRENT_WRITES,
                    "queue_limit": _WRITE_QUEUE_LIMIT,
                },
                headers=_write_backpressure_headers(),
            )
        _WRITE_BACKPRESSURE_WAITING += 1

    acquired = False
    try:
        try:
            await asyncio.wait_for(_WRITE_SEMAPHORE.acquire(), timeout=_WRITE_ACQUIRE_TIMEOUT_SECONDS)
            acquired = True
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "write_backpressure_timeout",
                    "operation": operation,
                    "max_concurrent_writes": _MAX_CONCURRENT_WRITES,
                },
                headers=_write_backpressure_headers(),
            ) from None

        async with _WRITE_BACKPRESSURE_LOCK:
            _WRITE_BACKPRESSURE_WAITING -= 1
            _WRITE_BACKPRESSURE_ACTIVE += 1

        try:
            yield
        finally:
            async with _WRITE_BACKPRESSURE_LOCK:
                _WRITE_BACKPRESSURE_ACTIVE -= 1
    finally:
        if not acquired:
            async with _WRITE_BACKPRESSURE_LOCK:
                _WRITE_BACKPRESSURE_WAITING = max(_WRITE_BACKPRESSURE_WAITING - 1, 0)
        else:
            _WRITE_SEMAPHORE.release()


async def _run_bounded_write(operation: str, func, *args, **kwargs):
    async with _bounded_write(operation):
        return await run_in_threadpool(func, *args, **kwargs)


def _read_backpressure_headers() -> dict[str, str]:
    return {"Retry-After": str(_READ_RETRY_AFTER_SECONDS)}


@asynccontextmanager
async def _bounded_read(route_operation: str):
    """Run sync read-only work away from the event loop with bounded admission."""

    global _READ_BACKPRESSURE_ACTIVE, _READ_BACKPRESSURE_WAITING

    async with _READ_BACKPRESSURE_LOCK:
        queued_or_running = _READ_BACKPRESSURE_ACTIVE + _READ_BACKPRESSURE_WAITING
        queue_capacity = _MAX_CONCURRENT_READS + _READ_QUEUE_LIMIT
        if queued_or_running >= queue_capacity:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "read_backpressure",
                    "operation": route_operation,
                    "active": _READ_BACKPRESSURE_ACTIVE,
                    "waiting": _READ_BACKPRESSURE_WAITING,
                    "max_concurrent_reads": _MAX_CONCURRENT_READS,
                    "queue_limit": _READ_QUEUE_LIMIT,
                },
                headers=_read_backpressure_headers(),
            )
        _READ_BACKPRESSURE_WAITING += 1

    acquired = False
    try:
        try:
            await asyncio.wait_for(_READ_SEMAPHORE.acquire(), timeout=_READ_ACQUIRE_TIMEOUT_SECONDS)
            acquired = True
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "read_backpressure_timeout",
                    "operation": route_operation,
                    "max_concurrent_reads": _MAX_CONCURRENT_READS,
                },
                headers=_read_backpressure_headers(),
            ) from None

        async with _READ_BACKPRESSURE_LOCK:
            _READ_BACKPRESSURE_WAITING -= 1
            _READ_BACKPRESSURE_ACTIVE += 1

        try:
            yield
        finally:
            async with _READ_BACKPRESSURE_LOCK:
                _READ_BACKPRESSURE_ACTIVE -= 1
    finally:
        if not acquired:
            async with _READ_BACKPRESSURE_LOCK:
                _READ_BACKPRESSURE_WAITING = max(_READ_BACKPRESSURE_WAITING - 1, 0)
        else:
            _READ_SEMAPHORE.release()


async def _run_bounded_read(route_operation: str, func, *args, **kwargs):
    async with _bounded_read(route_operation):
        return await run_in_threadpool(func, *args, **kwargs)


_PROJECT_REGISTRY = get_default_project_registry()


_MEMORY_SERVICE_LOCK = threading.RLock()


_MEMORY_SERVICES: dict[str, SQLiteMemoryService] = {}


_LINK_STORE_LOCK = threading.RLock()


_JSON_STORE_LOCKS: dict[str, threading.RLock] = {}


_JSON_STORE_LOCKS_LOCK = threading.RLock()


_JSON_REPLACE_RETRY_DELAYS = (0.025, 0.05, 0.1, 0.2, 0.4, 0.8)


def _json_store_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _JSON_STORE_LOCKS_LOCK:
        lock = _JSON_STORE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _JSON_STORE_LOCKS[key] = lock
        return lock


_MEMORY_PULSE_BUS = MemoryPulseBus(project_normalizer=lambda project: _canonical_project(project))


def _emit_memory_pulse(node_id: Any, project: str | None = None) -> None:
    _MEMORY_PULSE_BUS.emit_pulse(str(node_id or ""), project)


def _memory_store_state():
    return memory_runtime_storage_state(settings.runtime_dir, switch_wired=True)


def _memory_store_is_authoritative() -> bool:
    return _memory_store_state().configured_mode == MemoryStoreMode.SQLITE_AUTHORITATIVE.value


def _memory_service() -> SQLiteMemoryService:
    config = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir)
    key = str(config.database_path)
    with _MEMORY_SERVICE_LOCK:
        service = _MEMORY_SERVICES.get(key)
        if service is None:
            service = SQLiteMemoryService(config.database_path)
            _MEMORY_SERVICES[key] = service
        return service


def _memory_link_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "memory-links.json"


_LINK_STORAGE_METADATA_KEY = "__bhm_link_storage_metadata"


def _link_record_for_runtime(record: Mapping[str, Any]) -> dict[str, Any]:
    """Expose legacy link metadata without making a sidecar authoritative."""

    item = dict(record)
    metadata = item.get("metadata")
    if not isinstance(metadata, Mapping):
        item["metadata"] = {}
        return item
    raw_metadata = dict(metadata)
    legacy_metadata = raw_metadata.get("legacy_metadata")
    if isinstance(legacy_metadata, Mapping) and isinstance(raw_metadata.get("sidecar_provenance"), Mapping):
        item["metadata"] = dict(legacy_metadata)
        # Public serializers omit this marker. It lets a future update retain
        # original migration provenance inside authoritative SQLite.
        item[_LINK_STORAGE_METADATA_KEY] = raw_metadata
    else:
        item["metadata"] = raw_metadata
    return item


def _link_record_for_storage(record: Mapping[str, Any]) -> dict[str, Any]:
    """Rebind a runtime link record to its canonical SQLite metadata shape."""

    item = {key: value for key, value in dict(record).items() if key != _LINK_STORAGE_METADATA_KEY}
    visible_metadata = item.get("metadata")
    if not isinstance(visible_metadata, Mapping):
        visible_metadata = {}
    original = record.get(_LINK_STORAGE_METADATA_KEY)
    if isinstance(original, Mapping) and isinstance(original.get("legacy_metadata"), Mapping):
        preserved = dict(original)
        preserved["legacy_metadata"] = dict(visible_metadata)
        item["metadata"] = preserved
    else:
        item["metadata"] = dict(visible_metadata)
    return item


def _task_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "tasks.json"


def _load_live_memories() -> list[dict]:
    try:
        return _memory_service().load_records()
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc


def _project_aliases(project: str | None) -> set[str]:
    if not project:
        return set()
    return _PROJECT_REGISTRY.accepted_values(project)


def _canonical_project(project: str | None) -> str:
    return _PROJECT_REGISTRY.canonicalize(project)


def _find_live_memory(memory_id: str, project: str | None = None) -> dict | None:
    try:
        scoped_project = _canonical_project(project) if project else None
        record = _memory_service().get_record(memory_id, project=scoped_project)
        if record is not None:
            _emit_memory_pulse(record.get("source_id"), str(record.get("project") or ""))
            return record
    except MemoryServiceNotReady:
        pass
    accepted_projects = _project_aliases(project)
    for item in _load_live_memories():
        if item.get("source_id") != memory_id:
            continue
        if accepted_projects and item.get("project") not in accepted_projects:
            continue
        _emit_memory_pulse(item.get("source_id"), str(item.get("project") or ""))
        return item
    return None


def _load_tasks() -> list[dict]:
    path = _task_store_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def _save_tasks(items: list[dict]) -> Path:
    return _write_json_atomic(_task_store_path(), items)


def _write_json_atomic(path: Path, payload: object) -> Path:
    with _json_store_lock(path):
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        retry_delays = (*_JSON_REPLACE_RETRY_DELAYS, None)
        for retry_delay in retry_delays:
            try:
                replace_bytes_safely(path, encoded)
                break
            except PermissionError:
                if retry_delay is None:
                    raise
                time.sleep(retry_delay)
            except OSError as exc:
                if retry_delay is None or getattr(exc, "winerror", None) not in {5, 32}:
                    raise
                time.sleep(retry_delay)
    return path


def _safe_json_list(path: Path, repair_trailing_data: bool = False) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        if not repair_trailing_data:
            raise
        decoder = json.JSONDecoder()
        value, end = decoder.raw_decode(raw)
        if raw[end:].strip():
            _write_json_atomic(path, value)
    if not isinstance(value, list):
        raise ValueError(f"expected JSON list in {path}")
    return value


def _load_memory_links() -> list[dict]:
    if not _memory_store_is_authoritative():
        return _safe_json_list(_memory_link_store_path())
    try:
        return [_link_record_for_runtime(record) for record in _memory_service().list_links(limit=None)]
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc


def _save_memory_links(items: list[dict]) -> Path:
    if not _memory_store_is_authoritative():
        return _write_json_atomic(_memory_link_store_path(), items)
    try:
        with _LINK_STORE_LOCK:
            _memory_service().replace_link_records([_link_record_for_storage(item) for item in items])
        return _memory_service().path
    except MemoryServiceNotReady as exc:
        raise StorageNotReady(str(exc)) from exc


def _project_projection_route(project_name: str):
    """Return the authority-bound local route for one project.

    Bound/retired projects deliberately do not reopen their old local
    collection if SQLite routing evidence becomes unavailable.  Unbound
    projects retain the established legacy local route.
    """

    legacy_collection = local_collection_name(project_name)
    database_path = resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path
    return resolve_projection_route(
        project=project_name,
        legacy_collection=legacy_collection,
        database=database_path,
        embedding_model=settings.mem0_embedding_model,
        vector_dimensions=settings.mem0_embedding_dims,
        distance="Cosine",
    )
