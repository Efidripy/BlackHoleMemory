"""Domain module app_core_hook: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from . import app_runtime as rt
from .app_runtime import _env_float, _env_int, _safe_exception_text, _write_json_atomic
from .api_models import BhmHookRequest, BhmHookCompactRequest
from .config import settings
from .resource_limits import PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS
from .caller_auth import authorize_projects
from .ui_origin_policy import request_host_parts
from .runtime_storage import resolve_runtime_storage_config
from .memory_service import MemoryServiceNotReady
from .utility_feedback import utility_report
from .utility_feedback_store import load_utility_events
from . import memory_contracts as _memory_contracts
from .hook_queue import HookJobQueue
from .observation_contract import ObservationIngressV1
from .observation_store import ObservationIdCollision
from .observation_store import ObservationStore
from .retrieval_funnel import RetrievalFunnel
from .repository_index import probe_repository_state
from .llm_resource_governor import GovernorConfig
from .llm_resource_governor import LLMResourceGovernor


MetadataActionability = _memory_contracts.MetadataActionability


MetadataDomain = _memory_contracts.MetadataDomain


MetadataLanguage = _memory_contracts.MetadataLanguage


MetadataLifecycle = _memory_contracts.MetadataLifecycle


MetadataPriority = _memory_contracts.MetadataPriority


MetadataProvenance = _memory_contracts.MetadataProvenance


MetadataRetention = _memory_contracts.MetadataRetention


MetadataScope = _memory_contracts.MetadataScope


MetadataSemanticType = _memory_contracts.MetadataSemanticType


MetadataSensitivity = _memory_contracts.MetadataSensitivity


MetadataStakeholder = _memory_contracts.MetadataStakeholder


MetadataVerification = _memory_contracts.MetadataVerification


_INFRA_SPAWNED_PIDS: set[int] = set()


_INFRA_SPAWNED_PIDS_LOCK = threading.RLock()


def _env_enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().casefold() in {"1", "true", "yes", "on"}


_HOOK_QUEUE_CAPACITY = _env_int("BHM_HOOK_QUEUE_CAPACITY", 128, 1)


_HOOK_COMPACT_WORKERS = _env_int("BHM_HOOK_COMPACT_WORKERS", 1, 1)


_HOOK_IDLE_WORKERS = _env_int("BHM_HOOK_IDLE_WORKERS", 1, 1)


_HOOK_QUEUE_LEASE_SECONDS = _env_float("BHM_HOOK_QUEUE_LEASE_SECONDS", 120.0, 5.0)


_HOOK_QUEUE_DRAIN_SECONDS = _env_float("BHM_HOOK_QUEUE_DRAIN_SECONDS", 10.0, 0.1)


_HOOK_QUEUE_STORES: dict[str, HookJobQueue] = {}


_HOOK_QUEUE_STORES_LOCK = threading.RLock()


_HOOK_QUEUE_STOP_EVENT: asyncio.Event | None = None


_CUSTOM_REDACTION_MAX_PATTERNS = 16


_BOOT_REPORT_PATH = settings.runtime_dir / "infra" / "boot_report.json"


_RETRIEVAL_FUNNEL = RetrievalFunnel()


_LLM_GOVERNOR_LOCK = threading.RLock()


_LLM_GOVERNOR: LLMResourceGovernor | None = None


_SEMANTIC_READINESS_GATE_ENABLED = _env_enabled("BHM_SEMANTIC_READINESS_GATE", False)


def _hook_queue_path() -> Path:
    return settings.runtime_dir / "live-memory" / "hook-jobs.sqlite3"


def _hook_queue() -> HookJobQueue:
    path = _hook_queue_path().resolve()
    key = str(path)
    with _HOOK_QUEUE_STORES_LOCK:
        queue = _HOOK_QUEUE_STORES.get(key)
        if queue is None:
            queue = HookJobQueue(path, capacity=_HOOK_QUEUE_CAPACITY)
            _HOOK_QUEUE_STORES[key] = queue
        return queue


def _ensure_hook_request_identity(request: BaseModel) -> BaseModel:
    updates: dict[str, Any] = {}
    if not getattr(request, "eventId", None):
        updates["eventId"] = f"obs_bhm_{uuid.uuid4().hex}"
    if not getattr(request, "correlationId", None):
        updates["correlationId"] = str(getattr(request, "sessionId", "") or updates.get("eventId") or "")
    return request.model_copy(update=updates) if updates else request


def _request_host_parts(host_header: str | None) -> tuple[str, str] | None:
    return request_host_parts(host_header, canonical_port=settings.port)


_OBSERVATION_STORE_LOCK = threading.RLock()


_OBSERVATION_SQLITE_STORES: dict[str, ObservationStore] = {}


def _request_surface(request: Request) -> str:
    caller_surface = str(request.headers.get("x-bhm-caller-surface") or "").strip().lower()
    return "mcp" if caller_surface == "mcp" else "rest"


def _request_actor(request: Request) -> str:
    principal = getattr(request.state, "bhm_caller_principal", None)
    return str(getattr(principal, "caller_id", "authenticated-caller"))


def _shared_memory_read_enabled() -> bool:
    """Return an opt-in switch; the safe default remains no shared-data route."""

    return str(os.getenv("BHM_SHARED_MEMORY_READ_ENABLED", "") or "").strip().casefold() in {"1", "true", "yes"}


def _utility_feedback_report(
    *,
    project: str,
    as_of: str,
    half_life_days: float,
    min_samples: int,
) -> dict[str, Any]:
    """Build a deterministic, project-scoped report from immutable events."""

    canonical_project = rt._canonical_project(project)
    try:
        report = utility_report(
            load_utility_events(rt._memory_service(), project=canonical_project),
            as_of=as_of,
            half_life_days=half_life_days,
            min_samples=min_samples,
        )
    except (MemoryServiceNotReady, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "utility_feedback_report_invalid", "reason": _safe_exception_text(exc)},
        ) from exc
    return {
        **report,
        "project": canonical_project,
        "lifecycle_action": "none",
        "side_effects": {
            "read_only": True,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "projection_mutation": False,
        },
    }


def _governed_consolidation_database_path() -> Path:
    return resolve_runtime_storage_config(runtime_dir=settings.runtime_dir).database_path


def _observe_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "observations.sqlite3"


def _observation_store() -> ObservationStore:
    path = _observe_store_path().resolve()
    key = str(path)
    with _OBSERVATION_STORE_LOCK:
        store = _OBSERVATION_SQLITE_STORES.get(key)
        if store is None:
            store = ObservationStore(path)
            _OBSERVATION_SQLITE_STORES[key] = store
        return store


def _checkpoint_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "checkpoints.json"


def _project_map_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "project-maps.json"


def _session_record_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "session-records.json"


def _task_context_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "task-contexts.json"


def _risk_register_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "risk-registers.json"


def _validation_snapshot_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "validation-snapshots.json"


def _policy_profile_store_path() -> Path:
    return settings.runtime_dir / "live-memory" / "policy-profile.json"


def _fast_semantic_runtime_slo_status() -> str:
    """Return the semantic-request SLO state without re-entering health routes.

    ``bhm/health/slo`` intentionally performs a full readiness/cutover probe,
    which is appropriate for an operator endpoint but far too expensive to
    call again from an already authenticated code-search request.  The
    readiness gate has already verified the authoritative store and outbox;
    this fallback preserves the same fail-closed projection/provider boundary
    when the gate is disabled.
    """

    try:
        if not rt._memory_store_is_authoritative():
            return "breached"
        outbox = rt._memory_service().outbox_status()
        pending = max(int(outbox.get("pending") or 0), 0) + max(int(outbox.get("processing") or 0), 0)
        failed = max(int(outbox.get("failed") or 0), 0) + max(int(outbox.get("dead_letter") or 0), 0)
        if pending or failed:
            return "breached"
        if not bool(rt._get_provider_warmup_status().get("ready")):
            return "breached"
        return "healthy"
    except (MemoryServiceNotReady, OSError, sqlite3.Error, ValueError, TypeError):
        return "breached"


def _project_matches(record_project: Any, requested_project: str | None) -> bool:
    """Match a stored project against a canonical id or one of its aliases."""

    if not requested_project:
        return True
    return str(record_project or "").strip() in rt._project_aliases(requested_project)


def _authorize_resource_project(request: Request, project: Any) -> None:
    """Re-check ownership after loading a project-bearing global resource."""

    principal = getattr(request.state, "bhm_caller_principal", None)
    raw_project = str(project or "").strip()
    if principal is None:
        raise HTTPException(status_code=401, detail={"code": "caller_auth_required"})
    if not raw_project:
        raise HTTPException(
            status_code=403,
            detail={"code": "caller_resource_project_missing"},
        )
    project_error = authorize_projects(
        principal,
        (rt._canonical_project(raw_project),),
        require_explicit=True,
    )
    if project_error:
        raise HTTPException(status_code=403, detail={"code": project_error})


def _serialize_checkpoint_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "checkpoint_type": record.get("checkpoint_type"),
        "title": record.get("title"),
        "content": record.get("content"),
        "done": record.get("done") or "",
        "next": record.get("next") or "",
        "checks": record.get("checks") or "",
        "risks": record.get("risks") or "",
        "concepts": record.get("concepts") or [],
        "files": record.get("files") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_task_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "task_id": record.get("task_id"),
        "title": record.get("title") or "",
        "intent": record.get("intent") or "",
        "scope_in": record.get("scope_in") or [],
        "scope_out": record.get("scope_out") or [],
        "repo": record.get("repo") or "",
        "owner": record.get("owner") or "",
        "status": record.get("status") or "open",
        "session_id": record.get("session_id") or "",
        "correlation_id": record.get("correlation_id") or "",
        "files_touched": record.get("files_touched") or [],
        "session_record_id": record.get("session_record_id"),
        "memory_id": record.get("memory_id"),
        "done": record.get("done") or "",
        "next": record.get("next") or "",
        "checks": record.get("checks") or "",
        "risks": record.get("risks") or "",
        "decisions": record.get("decisions") or "",
        "validation": record.get("validation") or "",
        "opened_at": record.get("opened_at"),
        "closed_at": record.get("closed_at"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_task_context_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "current_task": record.get("current_task") or "",
        "status": record.get("status") or "",
        "pending_items": record.get("pending_items") or "",
        "guidance": record.get("guidance") or "",
        "next_step": record.get("next_step") or "",
        "files_touched": record.get("files_touched") or [],
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


def _serialize_risk_register_record(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "project": record.get("project"),
        "title": record.get("title"),
        "summary": record.get("summary") or "",
        "top_risks": record.get("top_risks") or [],
        "mitigations": record.get("mitigations") or [],
        "owner": record.get("owner") or "",
        "memory_id": record.get("memory_id"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "metadata": record.get("metadata") or {},
    }


_JSON_STORE_CACHE: dict[str, tuple[int, int, list[dict]]] = {}
_JSON_STORE_LOCK = threading.Lock()


def _load_json_store(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        stat = path.stat()
    except OSError:
        return []
    key = str(path.resolve())
    with _JSON_STORE_LOCK:
        cached = _JSON_STORE_CACHE.get(key)
        if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
    try:
        raw = path.read_text(encoding="utf-8").strip()
        data = json.loads(raw) if raw else []
    except (OSError, ValueError):
        return []
    with _JSON_STORE_LOCK:
        _JSON_STORE_CACHE[key] = (stat.st_mtime_ns, stat.st_size, data)
    return data


def _load_checkpoints() -> list[dict]:
    return _load_json_store(_checkpoint_store_path())


def _load_project_maps() -> list[dict]:
    return _load_json_store(_project_map_store_path())


def _save_project_maps(items: list[dict]) -> Path:
    return _write_json_atomic(_project_map_store_path(), items)


def _load_session_records() -> list[dict]:
    return _load_json_store(_session_record_store_path())


def _load_task_contexts() -> list[dict]:
    path = _task_context_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _load_risk_registers() -> list[dict]:
    path = _risk_register_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _load_validation_snapshots() -> list[dict]:
    path = _validation_snapshot_store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _load_policy_profile() -> dict:
    path = _policy_profile_store_path()
    if not path.exists():
        return {
            "max_content_chars": 8000,
            "max_lines": 120,
            "require_project": True,
            "require_memory_type": False,
            "block_secret_like": True,
            "block_raw_logs": False,
            "updated_at": None,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def _load_policy_profile_snapshot() -> dict[str, Any]:
    path = _policy_profile_store_path()
    return {
        "path": str(path),
        "exists": path.exists(),
        "profile": _load_policy_profile(),
    }


def _read_boot_report() -> dict[str, Any]:
    try:
        if not _BOOT_REPORT_PATH.exists():
            return {"status": "missing"}
        data = json.loads(_BOOT_REPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "ERROR", "error": "boot_report_unavailable", "timestamp": rt._utc_now_iso()}
    if isinstance(data, dict):
        return data
    return {"status": "ERROR", "error": "boot report is not a JSON object", "timestamp": rt._utc_now_iso()}


def _register_infra_pid(pid: int | str | None) -> None:
    try:
        normalized = int(pid or 0)
    except (TypeError, ValueError):
        return
    if normalized <= 0 or normalized == os.getpid():
        return
    with _INFRA_SPAWNED_PIDS_LOCK:
        _INFRA_SPAWNED_PIDS.add(normalized)


def _load_observations() -> list[dict]:
    with _OBSERVATION_STORE_LOCK:
        return _observation_store().load()


def _append_observations(items: list[dict]) -> Path:
    """Append observations to the authoritative SQLite journal."""

    with _OBSERVATION_STORE_LOCK:
        store = _observation_store()
        store.initialize()
        store.append_many(items)
        return store.path


def _append_observation(item: dict) -> None:
    with _OBSERVATION_STORE_LOCK:
        try:
            _observation_store().append(item)
        except ObservationIdCollision as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "observation_event_id_collision",
                    "eventId": exc.event_id,
                },
            ) from exc


def _query_tokens(query: str) -> list[str]:
    return [token for token in re.findall(r"[a-zA-Z0-9_-]+", query.lower()) if token]


def _safe_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _memory_type_weight(item: dict) -> float:
    metadata = item.get("metadata") or {}
    tags = set(metadata.get("tags") or [])
    title = (metadata.get("raw_title") or "").lower()

    weight = 0.0
    if "hybrid-session" in tags or "hybrid session" in title:
        weight += 2.5
    if "conversation" in tags:
        weight += 0.5
    if metadata.get("memory_type") == "architecture":
        weight += 0.5
    if metadata.get("memory_type") == "fact":
        weight += 0.2
    if metadata.get("memory_type") == "bug":
        weight += 0.2
    if metadata.get("project"):
        weight += 0.3
    return weight


def _query_intent_weight(query: str, item: dict) -> float:
    metadata = item.get("metadata") or {}
    title = (metadata.get("raw_title") or "").lower()
    tags = set(metadata.get("tags") or [])
    tokens = set(_query_tokens(query))
    memory_type = metadata.get("memory_type")

    score = 0.0
    checkpoint_like = {"checkpoint", "session", "hybrid", "workflow"}
    if tokens & checkpoint_like:
        if "checkpoint" in title:
            score += 1.5
        if "hybrid-session" in tags:
            score += 1.5
    else:
        if memory_type in {"fact", "bug", "pattern"}:
            score += 1.4
        if "hybrid-session" in tags:
            score -= 0.3
        if "checkpoint" in title:
            score -= 0.6

    if "role" in tokens and "capability" in tokens and "hybrid-session" in tags:
        score += 1.5

    if {"codex", "mcp", "tools"}.issubset(tokens):
        content = (item.get("memory") or "").lower()
        if "tool_search" in content or "plugin" in title or "plugin" in content:
            score += 1.8
        if "figma mcp" in tags or "codex plugin" in tags:
            score += 3.2
        if "mcp preflight" in tags or "oz mcp list" in tags:
            score -= 1.3
        if "connectivity" in tags:
            score -= 0.4

    if {"workspace", "memory", "checkpoint"}.issubset(tokens):
        if "workspace-layers" in tags:
            score += 4.0
        if "project.md" in title or "project.md" in (item.get("memory") or "").lower():
            score += 3.0
        if "hub" in tags:
            score += 1.5
        if "checkpoint" in title and "workspace-layers" not in tags:
            score -= 0.4

    if {"blackholememory", "checkpoint"}.issubset(tokens):
        content = (item.get("memory") or "").lower()
        if "created new blackholememory repo" in content:
            score += 3.0
        if "bootstrap" in content or "bootstrap" in title:
            score += 1.5
        if "initial qdrant compose" in content:
            score += 1.0

    return score


def _mem0_user_ids_for_search(user_id: str) -> list[str]:
    return [user_id]


def _lexical_signal(query: str, item: dict) -> float:
    metadata = item.get("metadata") or {}
    text = (item.get("memory") or "").lower()
    title = (metadata.get("raw_title") or "").lower()
    tag_blob = " ".join(metadata.get("tags") or []).lower()
    tokens = _query_tokens(query)

    score = 0.0
    if query.lower() in title:
        score += 2.0
    if query.lower() in text:
        score += 1.0

    for token in tokens:
        if token in title:
            score += 0.8
        if token in tag_blob:
            score += 0.8
        if token in text:
            score += 0.25

    return score


def _collapse_duplicates(results: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for item in results:
        key = item.get("hash") or item.get("memory") or item.get("id")
        current = grouped.get(key)
        if current is None:
            grouped[key] = item
            continue

        current_project = (current.get("metadata") or {}).get("project")
        item_project = (item.get("metadata") or {}).get("project")
        current_created = _safe_datetime(current.get("created_at"))
        item_created = _safe_datetime(item.get("created_at"))

        replace = False
        if not current_project and item_project:
            replace = True
        elif current_project == item_project and item_created < current_created:
            replace = True

        if replace:
            grouped[key] = item

    return list(grouped.values())


_HOOK_TRANSIT_SOURCE_LIMIT = 12


def _dedupe_text(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _coerce_hook_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in (chunk.strip() for chunk in re.split(r"[,\n]+", value)) if part]
    if isinstance(value, (list, tuple)):
        return _dedupe_text([str(item) for item in value if item is not None])
    return []


def _hook_data_value(request: BhmHookRequest, *keys: str) -> Any:
    data = request.data or {}
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _hook_transit_items(request: BhmHookCompactRequest) -> list[Any]:
    explicit_values = [
        request.transit_buffer,
        _hook_data_value(request, "transit_buffer", "transitBuffer"),
        _hook_data_value(request, "transit", "buffer", "current_buffer", "currentBuffer"),
        _hook_data_value(request, "messages", "history", "events", "transcript", "context"),
    ]
    items: list[Any] = []
    for value in explicit_values:
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            items.extend(value)
        else:
            items.append(value)
    if not items and request.data:
        items.append(dict(request.data))
    return items[:_HOOK_TRANSIT_SOURCE_LIMIT]


def _extract_hook_source_ids(request: BhmHookCompactRequest, transit_items: list[Any]) -> list[str]:
    source_ids: list[str] = []
    source_ids.extend(_coerce_hook_text_list(request.source_ids))
    for key in ("source_ids", "sourceIds", "memory_ids", "memoryIds"):
        source_ids.extend(_coerce_hook_text_list(_hook_data_value(request, key)))
    for item in transit_items:
        if not isinstance(item, dict):
            continue
        source_ids.extend(
            _coerce_hook_text_list(
                item.get("source_id")
                or item.get("sourceId")
                or item.get("memory_id")
                or item.get("memoryId")
            )
        )
    return _dedupe_text(source_ids)


def _lifecycle_source_ids(request: BhmHookRequest | ObservationIngressV1) -> list[str]:
    """Read only explicit source references for a content-free proposal.

    Lifecycle policy never infers source IDs from transcript text.  A compact
    hook may use its established typed fields; every other ingress must supply
    one of the explicit ID fields in structured data.
    """

    if isinstance(request, BhmHookCompactRequest):
        return _extract_hook_source_ids(request, _hook_transit_items(request))
    data = request.data
    if not isinstance(data, Mapping):
        return []
    source_ids: list[str] = []
    for key in ("source_ids", "sourceIds", "memory_ids", "memoryIds"):
        source_ids.extend(_coerce_hook_text_list(data.get(key)))
    return _dedupe_text(source_ids)


def _list_checkpoints(project: str | None, checkpoint_type: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    items = _load_checkpoints()
    if project:
        items = [item for item in items if item.get("project") == project]
    if checkpoint_type:
        items = [item for item in items if item.get("checkpoint_type") == checkpoint_type]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _get_latest_checkpoint(project: str, checkpoint_type: str | None = None) -> dict:
    items, total = _list_checkpoints(project=project, checkpoint_type=checkpoint_type, limit=1, offset=0)
    if total < 1 or not items:
        raise HTTPException(status_code=404, detail="checkpoint not found")
    return items[0]


def _list_tasks(project: str | None, status: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    project_name = rt._canonical_project(project) if project else None
    items = [
        item
        for item in rt._load_tasks()
        if (not project_name or item.get("project") == project_name)
        and (not status or item.get("status") == status)
    ]
    items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    total = len(items)
    start = max(offset, 0)
    end = start + max(min(limit, 200), 1)
    return items[start:end], total


def _get_task_context(project: str) -> dict:
    items = _load_task_contexts()
    for item in items:
        if item.get("project") == project:
            return item
    raise HTTPException(status_code=404, detail="task context not found")


def _get_risk_register(project: str) -> dict:
    items = _load_risk_registers()
    for item in items:
        if item.get("project") == project:
            return item
    raise HTTPException(status_code=404, detail="risk register not found")


def _get_validation_snapshot(project: str) -> dict:
    items = _load_validation_snapshots()
    for item in items:
        if item.get("project") == project:
            return item
    raise HTTPException(status_code=404, detail="validation snapshot not found")


def _rerank_vector_results(query: str, results: list[dict]) -> list[dict]:
    reranked: list[tuple[float, dict]] = []
    for item in results:
        total = float(item.get("score") or 0.0)
        total += _lexical_signal(query, item)
        total += _memory_type_weight(item)
        total += _query_intent_weight(query, item)
        reranked.append((total, item))

    reranked.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in reranked]


def _llm_governor() -> LLMResourceGovernor:
    global _LLM_GOVERNOR
    with _LLM_GOVERNOR_LOCK:
        if _LLM_GOVERNOR is None:
            _LLM_GOVERNOR = LLMResourceGovernor(GovernorConfig.from_env())
        return _LLM_GOVERNOR


def _code_graph_query_root_id(project: str, root: str | Path) -> str:
    return str(probe_repository_state(Path(root), project=project).root_id)


def _read_git_head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=PROCESS_EXECUTION_DEFAULT_TIMEOUT_SECONDS,
    ).stdout.strip()
