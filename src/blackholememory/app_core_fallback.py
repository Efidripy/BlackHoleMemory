"""Domain module app_core_fallback: moved out of app.py (FIX-017). Cross-module calls go through module attributes."""

from __future__ import annotations

import time
import asyncio
import functools
import json
import logging
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from fastapi import HTTPException
from . import app_runtime as rt
from .app_runtime import ResponseTimeout, _env_float, _env_int
from .config import settings
from .resource_limits import BHM_FEDERATED_EMBEDDING_PREPARATION_TIMEOUT_SECONDS
from .resource_limits import BHM_FEDERATED_RETRIEVAL_CONTOUR_TIMEOUT_SECONDS
from .embedding_cache import EmbeddingCache
from .embedding_cache import embed_query_with_cache
from .mem0_adapter import get_project_mem0_memory
from .mem0_adapter import storage_runtime_state
from .project_registry import normalize_project_key
from .retrieval_filters import build_candidate_filters
from .exact_identifier_retrieval import ExactIdentifierIndex
from .exact_identifier_retrieval import build_exact_identifier_hits
from .exact_identifier_retrieval import exact_identifier_enabled
from .exact_identifier_retrieval import exact_identifier_tokens
from .typed_memory_contract import TypedMemoryContractUnavailable
from .typed_memory_contract import typed_memory_capability_available
from .typed_memory_contract import require_typed_memory_contract
from .typed_memory_contract import typed_memory_contract_enabled
from .temporal_contract import TemporalContractUnavailable
from .temporal_contract import TemporalValidationError
from .temporal_contract import normalize_temporal_fields
from .temporal_contract import normalize_temporal_timestamp
from .temporal_contract import require_temporal_contract
from .temporal_contract import temporal_intent_requested
from . import app_core
from . import app_core_graph
from . import app_core_hit


_LOGGER = logging.getLogger(__name__)


# Provider SDK calls are synchronous and may outlive the response deadline.
# Never put those calls on asyncio's shared default executor: a few cold or
# wedged embedding/Qdrant requests there can starve health, telemetry and
# recovery probes.  The gate is non-blocking so excess provider work fails
# closed into the existing fallback-grace path instead of queueing forever.
_RETRIEVAL_PROVIDER_WORKERS = _env_int("BHM_RETRIEVAL_PROVIDER_WORKERS", 4, 1)


_RETRIEVAL_PROVIDER_EXECUTOR = ThreadPoolExecutor(
    max_workers=_RETRIEVAL_PROVIDER_WORKERS,
    thread_name_prefix="bhm-retrieval-provider",
)


_RETRIEVAL_PROVIDER_SLOTS = threading.BoundedSemaphore(_RETRIEVAL_PROVIDER_WORKERS)


_FALLBACK_MODE_ENV = "BHM_FALLBACK_MODE"


_TELEMETRY_INTERVAL_SECONDS = 2.5


_FALLBACK_GRACE_ACTIVE_UNTIL = 0.0


class EmbeddingPreparationTimeout(ResponseTimeout):
    """A bounded federated query embedding did not complete in time."""


class RetrievalProviderCapacityExceeded(ResponseTimeout):
    """All bounded provider slots are occupied by still-running SDK calls."""


_EMBEDDING_CACHE_MAX_ENTRIES = _env_int("BHM_EMBEDDING_CACHE_MAX_ENTRIES", 256, 1)


_EMBEDDING_CACHE_TTL_SECONDS = _env_float("BHM_EMBEDDING_CACHE_TTL_SECONDS", 300.0, 0.1)


_QUERY_EMBEDDING_CACHE = EmbeddingCache(
    max_entries=_EMBEDDING_CACHE_MAX_ENTRIES,
    ttl_seconds=_EMBEDDING_CACHE_TTL_SECONDS,
)


def _configured_fallback_mode() -> str:
    value = os.getenv(_FALLBACK_MODE_ENV, "explicit").strip().lower()
    return value if value in {"explicit", "disabled"} else "disabled"


def _fallback_grace_active() -> bool:
    return time.monotonic() < _FALLBACK_GRACE_ACTIVE_UNTIL


def _mcp_registry_snapshot_path() -> Path:
    configured = os.getenv("BHM_MCP_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured) / "registry.json"
    return settings.runtime_dir / "mcp" / "registry.json"


def _read_json_snapshot(path: Path) -> Any:
    try:
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None


def _safe_fallback_provider_warmup() -> dict[str, Any]:
    status = rt._get_provider_warmup_status()
    return {
        "enabled": bool(status.get("enabled")),
        "ready": bool(status.get("ready")),
        "attempts": int(status.get("attempts") or 0),
        "updated_at": str(status.get("updated_at") or ""),
        "embedding_warmup_enabled": bool(status.get("embedding_warmup_enabled")),
        "embedding_ready": bool(status.get("embedding_ready")),
        "embedding_attempts": int(status.get("embedding_attempts") or 0),
        "embedding_phase": str(status.get("embedding_phase") or ""),
        "memory_warmup_enabled": bool(status.get("memory_warmup_enabled")),
        "memory_ready": bool(status.get("memory_ready")),
        "memory_projects_count": len(status.get("memory_projects") or []),
        "memory_skipped_projects_count": len(status.get("memory_skipped_projects") or []),
        "memory_phase": str(status.get("memory_phase") or ""),
        "error_present": any(
            bool(status.get(key))
            for key in ("last_error", "embedding_last_error", "memory_last_error")
        ),
    }


def _safe_fallback_storage() -> dict[str, Any]:
    state = storage_runtime_state().as_dict()
    return {
        "configured_mode": state.get("configured_mode"),
        "backend": state.get("backend"),
        "readiness": state.get("readiness"),
        "reason": state.get("reason"),
        "database_exists": bool(state.get("database_exists")),
        "database_schema_ready": bool(state.get("database_schema_ready")),
        "parity_confirmed": bool(state.get("parity_confirmed")),
        "writer_offline_confirmed": bool(state.get("writer_offline_confirmed")),
    }


def _fallback_grace_stage(reason: Exception) -> str:
    """Return a bounded, content-free phase for a degraded read response."""

    if isinstance(reason, EmbeddingPreparationTimeout):
        return "embedding_preparation"
    if isinstance(reason, ResponseTimeout):
        return "retrieval_contour"
    if isinstance(reason, ConnectionError):
        return "provider_transport"
    if isinstance(reason, HTTPException):
        return "provider_http"
    return "unknown"


def _fallback_grace_meta(route: str, reason: Exception) -> dict[str, Any]:
    global _FALLBACK_GRACE_ACTIVE_UNTIL
    _FALLBACK_GRACE_ACTIVE_UNTIL = time.monotonic() + (_TELEMETRY_INTERVAL_SECONDS * 2)
    print("[WARN] MCP transport slow, serving data from Fallback Grace disk snapshot", flush=True)
    return {
        "enabled": True,
        "mode": "degraded",
        "policy": _configured_fallback_mode(),
        "read_only": True,
        "route": route,
        "stage": _fallback_grace_stage(reason),
        "reason": type(reason).__name__,
        "registry": _read_json_snapshot(_mcp_registry_snapshot_path()),
        "provider_warmup": _safe_fallback_provider_warmup(),
        "storage": _safe_fallback_storage(),
    }


def _require_typed_memory_boundary(
    *,
    memory_class: app_core_hit.MemoryClass | str | None = None,
    event_role: app_core_hit.MemoryEventRole | str | None = None,
) -> None:
    """Reject typed intent until the additive contract is operator-enabled."""

    if memory_class is None and event_role is None:
        return
    try:
        require_typed_memory_contract(
            capability_available=typed_memory_capability_available(rt._memory_service().path)
        )
    except (TypedMemoryContractUnavailable, OSError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "typed_memory_contract_unavailable",
                "feature_flag": "BHM_TYPED_MEMORY_CONTRACT_ENABLED",
                "enabled": typed_memory_contract_enabled(),
                "reason": str(exc),
            },
        ) from exc


def _typed_projection_pushdown_ready() -> bool:
    """Return the explicit operator gate for equality pushdown into Qdrant."""

    raw = str(os.getenv("BHM_TYPED_MEMORY_PROJECTION_READY", "false") or "").strip().casefold()
    if raw not in {"1", "true", "yes", "on"}:
        return False
    if not typed_memory_contract_enabled():
        return False
    try:
        return typed_memory_capability_available(rt._memory_service().repository.path)
    except (OSError, sqlite3.Error):
        return False


def _require_temporal_memory_boundary(
    *,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    write_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    fields = dict(write_fields or {})
    requested = temporal_intent_requested(
        as_of=as_of,
        valid_from=valid_from,
        valid_to=valid_to,
        include_temporal_unknown=True if include_temporal_unknown else None,
        **fields,
    )
    if not requested:
        return {}
    try:
        require_temporal_contract(
            capability_available=app_core_graph._temporal_capability_available_for_runtime()
        )
        if fields:
            return normalize_temporal_fields({"metadata": fields})
        normalize_temporal_timestamp(as_of, "as_of", allow_none=False) if as_of is not None else None
        normalize_temporal_timestamp(valid_from, "query_valid_from")
        normalize_temporal_timestamp(valid_to, "query_valid_to")
        if valid_from is not None and valid_to is not None and normalize_temporal_timestamp(valid_from, "query_valid_from") >= normalize_temporal_timestamp(valid_to, "query_valid_to"):
            raise TemporalValidationError("query_valid_from must be earlier than query_valid_to")
        return {}
    except (TemporalContractUnavailable, TemporalValidationError, OSError, sqlite3.Error) as exc:
        code = str(exc)
        status = 409 if isinstance(exc, TemporalContractUnavailable) else 422
        raise HTTPException(status_code=status, detail={"code": code}) from exc


def _effective_search_project(project: str | None) -> str:
    """Resolve one fail-closed project scope for public memory search.

    Missing project input means the configured default project, never an
    unscoped search. ``global`` is a reserved storage contour, not a public
    tenant/project selector; global points are queried only as a secondary
    contour and are still filtered by the requested project.
    """

    raw_project = str(project or "").strip()
    candidate = rt._canonical_project(raw_project or settings.qdrant_collection)
    if normalize_project_key(candidate) == "global":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "global_scope_requires_explicit_internal_capability",
                "message": "global is a reserved BHM contour and is not a public project search scope",
            },
        )
    return candidate


def _cached_query_embedding(memory: Any, query: str) -> Any:
    model_key = f"{settings.mem0_embedding_model}:{settings.mem0_embedding_dims}"
    vector, _cache_hit = embed_query_with_cache(
        memory.embedding_model,
        query,
        model_key=model_key,
        cache=_QUERY_EMBEDDING_CACHE,
    )
    return vector


def _consume_late_read_worker(task: asyncio.Task[Any]) -> None:
    """Consume a detached read worker so its eventual exception is not lost."""

    try:
        task.result()
    except (asyncio.CancelledError, Exception):  # broad: consume late timed-out worker completion
        pass


async def _run_bounded_provider_call(worker: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Run one synchronous provider call outside asyncio's shared executor.

    Timed-out SDK calls cannot be force-cancelled.  A dedicated, non-growing
    executor plus a non-blocking slot gate prevents those late calls from
    starving health/readiness probes or building an unbounded queue.
    """

    if not _RETRIEVAL_PROVIDER_SLOTS.acquire(blocking=False):
        raise RetrievalProviderCapacityExceeded("retrieval provider capacity is busy")
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        _RETRIEVAL_PROVIDER_EXECUTOR,
        functools.partial(worker, *args, **kwargs),
    )

    def release_slot(_completed: Any) -> None:
        _RETRIEVAL_PROVIDER_SLOTS.release()

    future.add_done_callback(release_slot)
    return await future


async def _prepare_federated_query_embedding(project_name: str, query: str) -> tuple[Any, float]:
    """Prepare one reusable embedding without letting a cold provider block search.

    The provider is synchronous, so a timed-out thread cannot be stopped safely.
    The caller detaches from it after the bounded response deadline and follows
    the established explicit fallback-grace route.  The late read result is
    consumed and is never used to alter storage, ranking, or the response.
    """

    started = time.perf_counter()

    def prepare() -> Any:
        memory = get_project_mem0_memory(project_name)
        return _cached_query_embedding(memory, query)

    worker_task = asyncio.create_task(_run_bounded_provider_call(prepare))
    try:
        embedding = await asyncio.wait_for(
            asyncio.shield(worker_task),
            timeout=BHM_FEDERATED_EMBEDDING_PREPARATION_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        if worker_task.done():
            # A provider-raised timeout is already part of the existing
            # fallback taxonomy; preserve it instead of calling it a deadline.
            raise
        worker_task.add_done_callback(_consume_late_read_worker)
        raise EmbeddingPreparationTimeout("federated embedding preparation timed out") from exc
    except asyncio.CancelledError:
        worker_task.add_done_callback(_consume_late_read_worker)
        raise
    return embedding, (time.perf_counter() - started) * 1000.0


def _strict_retrieval_hits(
    hits: list[dict],
    *,
    project_name: str,
    memory_type: str | None = None,
    memory_class: app_core_hit.MemoryClass | str | None = None,
    event_role: app_core_hit.MemoryEventRole | str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    include_archived: bool = False,
    include_logs: bool = False,
    include_historical: bool = False,
    freshness_days: int | None = None,
    limit: int = 10,
) -> list[dict]:
    """Hydrate candidates from authoritative SQLite before exposing them.

    Qdrant/Mem0 payloads are only ranking hints.  A missing or stale point is
    suppressed rather than trusted, which keeps lifecycle, revision, project
    and typed-filter decisions on the SQLite authority boundary.
    """

    source_ids: list[str] = []
    hit_ids: dict[int, str] = {}
    for index, hit in enumerate(hits):
        metadata = hit.get("metadata") if isinstance(hit.get("metadata"), Mapping) else {}
        source_id = str(
            metadata.get("source_id")
            or hit.get("source_id")
            or hit.get("id")
            or ""
        ).strip()
        if source_id:
            hit_ids[index] = source_id
            if source_id not in source_ids:
                source_ids.append(source_id)
    if not source_ids:
        return []

    authoritative = {
        str(record.get("source_id") or record.get("id") or ""): record
        for record in rt._memory_service().get_records(
            source_ids,
            project=rt._canonical_project(project_name),
        )
    }
    accepted: list[dict] = []
    for index, hit in enumerate(hits):
        source_id = hit_ids.get(index)
        record = authoritative.get(source_id or "")
        if record is None or not app_core_hit._memory_matches_filters(
            record,
            project=project_name,
            memory_type=memory_type,
            memory_class=memory_class,
            event_role=event_role,
            concepts=concepts,
            files=files,
            domain=domain,
            semantic_type=semantic_type,
            priority=priority,
            as_of=as_of,
            valid_from=valid_from,
            valid_to=valid_to,
            include_temporal_unknown=include_temporal_unknown,
            include_archived=include_archived,
            include_logs=include_logs,
            include_historical=include_historical,
            freshness_days=freshness_days,
        ):
            continue
        enriched = dict(hit)
        metadata = dict(enriched.get("metadata") or {})
        authoritative_metadata = dict(record.get("metadata") or {})
        metadata.update(
            {
                "source_id": source_id,
                "project": record.get("project"),
                "memory_type": record.get("memory_type"),
                "memory_class": app_core._effective_memory_class(record),
                "memory_class_source": record.get("memory_class_source")
                or authoritative_metadata.get("memory_class_source"),
                "memory_class_confidence": record.get("memory_class_confidence")
                if record.get("memory_class_confidence") is not None
                else authoritative_metadata.get("memory_class_confidence"),
                "event_role": app_core._effective_event_role(record),
                "event_role_version": record.get("event_role_version")
                or authoritative_metadata.get("event_role_version")
                or app_core.EVENT_ROLE_SCHEMA_VERSION,
                "lifecycle": record.get("lifecycle") or authoritative_metadata.get("lifecycle"),
                "revision_id": record.get("revision_id") or authoritative_metadata.get("revision_id"),
                "content_sha256": record.get("content_sha256")
                or authoritative_metadata.get("content_sha256"),
                "observed_at": record.get("observed_at") or authoritative_metadata.get("observed_at"),
                "observed_at_source": record.get("observed_at_source")
                or authoritative_metadata.get("observed_at_source"),
                "valid_from": record.get("valid_from") or authoritative_metadata.get("valid_from"),
                "valid_to": record.get("valid_to") or authoritative_metadata.get("valid_to"),
                "open_interval": record.get("open_interval")
                if record.get("open_interval") is not None
                else authoritative_metadata.get("open_interval"),
                "supersedes_revision_id": record.get("supersedes_revision_id")
                or authoritative_metadata.get("supersedes_revision_id"),
                "source_episode_id": record.get("source_episode_id")
                or authoritative_metadata.get("source_episode_id"),
                "source_uri": record.get("source_uri") or authoritative_metadata.get("source_uri"),
                "source_digest": record.get("source_digest") or authoritative_metadata.get("source_digest"),
            }
        )
        enriched["metadata"] = metadata
        enriched["source_id"] = source_id
        enriched["content"] = record.get("content")
        enriched["memory"] = record.get("content")
        accepted.append(enriched)
    return accepted[: max(int(limit), 1)]


@dataclass(frozen=True)
class FederatedSearchOutcome:
    """Backward-compatible federated result plus content-free contour timing."""

    hits: list[dict]
    total: int
    contour_trace: dict[str, Any]

    def __iter__(self):
        """Preserve the established ``hits, total = await federated_search`` API."""

        yield self.hits
        yield self.total


@dataclass(frozen=True)
class _TimedRetrievalContour:
    name: str
    duration_ms: float
    result: Any = None
    error: Exception | None = None
    timed_out: bool = False


async def _run_timed_retrieval_contour(name: str, worker: Any, /, *args: Any, **kwargs: Any) -> _TimedRetrievalContour:
    """Run one read-only contour with a bounded response deadline.

    The Mem0/Qdrant client is synchronous and an in-flight SDK call cannot be
    forcefully cancelled from this async boundary.  The deadline therefore
    protects the caller and lets the sibling contour/fallback proceed; a late
    worker result is deliberately consumed without becoming a response.
    """

    started = time.perf_counter()
    worker_task = asyncio.create_task(_run_bounded_provider_call(worker, *args, **kwargs))
    try:
        result = await asyncio.wait_for(
            asyncio.shield(worker_task),
            timeout=BHM_FEDERATED_RETRIEVAL_CONTOUR_TIMEOUT_SECONDS,
        )
        return _TimedRetrievalContour(
            name=name,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            result=result,
        )
    except TimeoutError as exc:
        def consume_late_result(task: asyncio.Task[Any]) -> None:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):  # broad: consume late timed-out worker completion
                pass

        worker_task.add_done_callback(consume_late_result)
        return _TimedRetrievalContour(
            name=name,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            error=exc,
            timed_out=True,
        )
    except Exception as exc:  # broad: retrieval contour reports failures as data
        _LOGGER.warning("Retrieval contour failed (%s)", type(exc).__name__)
        return _TimedRetrievalContour(
            name=name,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            error=exc,
        )


def _federated_contour_trace(
    *,
    started: float,
    embedding_duration_ms: float,
    embedding_enabled: bool,
    contours: Sequence[_TimedRetrievalContour],
    exact_enabled: bool,
) -> dict[str, Any]:
    """Return only non-sensitive timings for one completed read-only search."""

    recorded = [
        {
            "name": contour.name,
            "enabled": True,
            "status": "timed_out" if contour.timed_out else "failed" if contour.error is not None else "completed",
            "duration_ms": app_core_hit._bounded_trace_duration_ms(contour.duration_ms),
            "deadline_ms": BHM_FEDERATED_RETRIEVAL_CONTOUR_TIMEOUT_SECONDS * 1000.0,
        }
        for contour in contours
    ]
    if not exact_enabled:
        recorded.append({"name": "exact_identifier", "enabled": False, "status": "disabled", "duration_ms": 0.0})
    return {
        "schema_version": "bhm.retrieval-contour-trace.v1",
        "total_duration_ms": app_core_hit._bounded_trace_duration_ms((time.perf_counter() - started) * 1000.0),
        "embedding": {
            "enabled": embedding_enabled,
            "status": "completed" if embedding_enabled else "skipped",
            "duration_ms": app_core_hit._bounded_trace_duration_ms(embedding_duration_ms),
            "deadline_ms": BHM_FEDERATED_EMBEDDING_PREPARATION_TIMEOUT_SECONDS * 1000.0,
        },
        "contours": recorded,
        "storage_mutation": False,
        "ranking_changed": False,
    }


def _build_exact_identifier_hits(
    *,
    query: str,
    project_name: str,
    candidate_count: int,
    memory_type: str | None,
    memory_class: app_core_hit.MemoryClass | str | None,
    event_role: app_core_hit.MemoryEventRole | str | None,
    domain: str | None,
    semantic_type: str | None,
    priority: str | None,
    as_of: str | None,
    valid_from: str | None,
    valid_to: str | None,
    include_temporal_unknown: bool,
    include_archived: bool,
    include_logs: bool,
    include_historical: bool,
) -> tuple[list[dict], str]:
    """Build and hydrate the opt-in exact lane from bounded SQLite candidates.

    The SQLite route is an operator-migrated project/token access index only.
    Hydration and Python token/filter checks remain authoritative, so a stale
    candidate or cross-project ID cannot become a result.
    This synchronous local worker overlaps its SQLite work with independent
    embedding/vector contours instead of extending their end-to-end tail.
    """

    service = rt._memory_service()
    candidate_ids: list[str] = []
    for token in exact_identifier_tokens(query, query=True):
        for source_id in service.find_exact_identifier_candidate_ids(
            project_name,
            token,
            limit=candidate_count - len(candidate_ids),
        ):
            if source_id not in candidate_ids:
                candidate_ids.append(source_id)
            if len(candidate_ids) >= candidate_count:
                break
        if len(candidate_ids) >= candidate_count:
            break
    authoritative_records = service.get_records(candidate_ids, project=project_name)
    exact_index = ExactIdentifierIndex.build(
        authoritative_records,
        include_record=lambda record: app_core_hit._memory_matches_filters(
            record,
            project=project_name,
            memory_type=memory_type,
            memory_class=memory_class,
            event_role=event_role,
            include_archived=include_archived,
            include_logs=include_logs,
            include_historical=include_historical,
            domain=domain,
            semantic_type=semantic_type,
            priority=priority,
            as_of=as_of,
            valid_from=valid_from,
            valid_to=valid_to,
            include_temporal_unknown=include_temporal_unknown,
        ),
    )
    exact_source_ids = exact_index.lookup(query, project=project_name, limit=candidate_count)
    hits = build_exact_identifier_hits(
        authoritative_records,
        exact_source_ids,
        project=project_name,
    )
    snapshot_digest = exact_index.snapshot_digest
    for hit in hits:
        hit.setdefault("metadata", {})["exact_identifier_snapshot_digest"] = snapshot_digest
    return hits, snapshot_digest


async def federated_search(
    query: str,
    project_name: str,
    limit: int = 5,
    offset: int = 0,
    *,
    memory_type: str | None = None,
    memory_class: app_core_hit.MemoryClass | str | None = None,
    event_role: app_core_hit.MemoryEventRole | str | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    domain: str | None = None,
    semantic_type: str | None = None,
    priority: str | None = None,
    as_of: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    include_temporal_unknown: bool = False,
    include_archived: bool = False,
    include_logs: bool = False,
    include_historical: bool = False,
    include_graph_expansion: bool = True,
    include_global: bool = True,
) -> FederatedSearchOutcome:
    started = time.perf_counter()
    _require_typed_memory_boundary(memory_class=memory_class, event_role=event_role)
    _require_temporal_memory_boundary(
        as_of=as_of,
        valid_from=valid_from,
        valid_to=valid_to,
        include_temporal_unknown=include_temporal_unknown,
    )
    project_name = _effective_search_project(project_name)
    include_historical = include_historical or (
        (event_role.value if isinstance(event_role, app_core_hit.MemoryEventRole) else str(event_role or "").strip().casefold())
        == app_core_hit.MemoryEventRole.TRACE.value
    )
    page_limit = max(min(limit, 200), 1)
    page_offset = max(offset, 0)
    typed_filter_requested = memory_class is not None or event_role is not None
    temporal_filter_requested = (
        as_of is not None
        or valid_from is not None
        or valid_to is not None
        or include_temporal_unknown
    )
    # Typed/temporal retrieval is an authoritative memory query.  Do not fan
    # it out through the code-graph JSON (which is a separate, rebuildable
    # surface and can be very large); graph expansion is reserved for the
    # normal unfiltered context path.
    if typed_filter_requested or temporal_filter_requested or include_historical:
        include_graph_expansion = False
    pushdown_ready = _typed_projection_pushdown_ready() if typed_filter_requested else False
    # Legacy projections may lack typed payload fields.  Keep a bounded wide
    # pool until an operator has proven V2 parity; final filtering remains
    # SQLite-authoritative and therefore cannot silently lose rare classes.
    candidate_count = max(
        page_limit + page_offset,
        20 if (pushdown_ready or not typed_filter_requested) and not temporal_filter_requested else 200,
    )
    candidate_filters = build_candidate_filters(
        user_id=settings.mem0_user_id,
        project_values=rt._project_aliases(project_name),
        memory_type=memory_type,
        memory_class=(
            memory_class.value if isinstance(memory_class, app_core_hit.MemoryClass) and pushdown_ready else
            str(memory_class).strip().lower() if memory_class is not None and pushdown_ready else None
        ),
        event_role=(
            event_role.value if isinstance(event_role, app_core_hit.MemoryEventRole) and pushdown_ready else
            str(event_role).strip().lower() if event_role is not None and pushdown_ready else None
        ),
        concepts=concepts or [],
        files=files or [],
        domain=domain,
        semantic_type=semantic_type,
        priority=priority,
        include_archived=include_archived,
        include_logs=include_logs,
        include_historical=include_historical,
    )
    exact_task: asyncio.Task[_TimedRetrievalContour] | None = None
    if query.strip() and exact_identifier_enabled() and exact_identifier_tokens(query, query=True):
        exact_task = asyncio.create_task(
            _run_timed_retrieval_contour(
                "exact_identifier",
                _build_exact_identifier_hits,
                query=query,
                project_name=project_name,
                candidate_count=candidate_count,
                memory_type=memory_type,
                memory_class=memory_class,
                event_role=event_role,
                domain=domain,
                semantic_type=semantic_type,
                priority=priority,
                as_of=as_of,
                valid_from=valid_from,
                valid_to=valid_to,
                include_temporal_unknown=include_temporal_unknown,
                include_archived=include_archived,
                include_logs=include_logs,
                include_historical=include_historical,
            )
    )

    query_embedding = None
    embedding_duration_ms = 0.0
    if query.strip():
        try:
            query_embedding, embedding_duration_ms = await _prepare_federated_query_embedding(project_name, query)
        except Exception as exc:  # broad: embedding failure must cancel late workers before propagating
            _LOGGER.warning("Federated embedding preparation failed (%s)", type(exc).__name__)
            if exact_task is not None:
                exact_task.add_done_callback(_consume_late_read_worker)
            raise

    local_task = asyncio.create_task(
        _run_timed_retrieval_contour(
            "local_vector",
            app_core_hit._search_memory_collection,
            query=query,
            project_name=project_name,
            context_origin=app_core_hit._VECTOR_CONTEXT_LOCAL,
            limit=candidate_count,
            candidate_filters=candidate_filters,
            query_embedding=query_embedding,
        )
    )
    tasks: list[asyncio.Task] = [local_task]
    if include_global:
        tasks.append(
            asyncio.create_task(
                _run_timed_retrieval_contour(
                    "global_vector",
                    app_core_hit._search_memory_collection,
                    query=query,
                    project_name=project_name,
                    context_origin=app_core_hit._VECTOR_CONTEXT_GLOBAL,
                    limit=candidate_count,
                    candidate_filters=candidate_filters,
                    query_embedding=query_embedding,
                )
            )
        )

    results = await asyncio.gather(*tasks)
    local_hits: list[dict] = []
    global_hits: list[dict] = []
    errors: list[Exception] = []
    contours = (app_core_hit._VECTOR_CONTEXT_LOCAL, app_core_hit._VECTOR_CONTEXT_GLOBAL) if include_global else (app_core_hit._VECTOR_CONTEXT_LOCAL,)
    for context_origin, contour in zip(contours, results, strict=True):
        if contour.error is not None:
            errors.append(contour.error)
            print(f"[WARN] BHM federated search {context_origin} contour failed: {contour.error}", flush=True)
            continue
        if context_origin == app_core_hit._VECTOR_CONTEXT_LOCAL:
            local_hits = contour.result
        else:
            global_hits = contour.result
    if errors and not local_hits and not global_hits:
        raise errors[0]

    now = datetime.now(timezone.utc)
    local_hits = app_core_hit._apply_decay_to_vector_hits(local_hits, now)
    global_hits = app_core_hit._apply_decay_to_vector_hits(global_hits, now)
    exact_hits: list[dict] = []
    exact_contour: _TimedRetrievalContour | None = None
    if exact_task is not None:
        exact_contour = await exact_task
        if exact_contour.error is not None:
            raise exact_contour.error
        exact_hits, _exact_snapshot_digest = exact_contour.result
        exact_hits = app_core_hit._apply_decay_to_vector_hits(exact_hits, now)
    combined_results = app_core_hit.merge_and_sort_hits([*local_hits, *exact_hits], global_hits)
    if temporal_filter_requested:
        # Temporal fields in Qdrant are an optional projection.  Hydrate from
        # SQLite before filtering so stale or missing projection payloads never
        # become authoritative for historical retrieval.
        combined_results = _strict_retrieval_hits(
            combined_results,
            project_name=project_name,
            memory_type=memory_type,
            memory_class=memory_class,
            event_role=event_role,
            concepts=concepts,
            files=files,
            domain=domain,
            semantic_type=semantic_type,
            priority=priority,
            as_of=as_of,
            valid_from=valid_from,
            valid_to=valid_to,
            include_temporal_unknown=include_temporal_unknown,
            include_archived=include_archived,
            include_logs=include_logs,
            include_historical=include_historical,
            limit=candidate_count,
        )
    filtered = [
        hit
        for hit in combined_results
        if app_core_hit._vector_hit_matches_filters(
            hit,
            project=project_name,
            memory_type=memory_type,
            memory_class=memory_class,
            event_role=event_role,
            concepts=concepts,
            files=files,
            domain=domain,
            semantic_type=semantic_type,
            priority=priority,
            as_of=as_of,
            valid_from=valid_from,
            valid_to=valid_to,
            include_temporal_unknown=include_temporal_unknown,
            include_archived=include_archived,
            include_logs=include_logs,
            include_historical=include_historical,
        )
    ]
    if include_graph_expansion:
        filtered = await app_core_hit._augment_hits_with_graph_expansion(filtered, project_name, now)
    ranked_hits = app_core_hit._rank_hybrid_vector_hits(query, filtered)
    total = len(ranked_hits)
    trace_contours = [*results]
    if exact_contour is not None:
        trace_contours.append(exact_contour)
    return FederatedSearchOutcome(
        hits=ranked_hits[page_offset : page_offset + page_limit],
        total=total,
        contour_trace=_federated_contour_trace(
            started=started,
            embedding_duration_ms=embedding_duration_ms,
            embedding_enabled=bool(query.strip()),
            contours=trace_contours,
            exact_enabled=exact_task is not None,
        ),
    )
