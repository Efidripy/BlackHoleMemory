from __future__ import annotations

# Third-party imports intentionally follow local telemetry/logging setup.
# ruff: noqa: E402

import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime
from datetime import timezone
from functools import lru_cache
from typing import Any

os.environ.setdefault("MEM0_TELEMETRY", "False")
logging.getLogger("posthog").setLevel(logging.CRITICAL + 1)
logging.getLogger("mem0.utils.spacy_models").setLevel(logging.ERROR)
logging.getLogger("mem0.vector_stores.qdrant").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message="Api key is used with an insecure connection.*")

class _LazyQdrantModels:
    _models: Any = None

    def __getattr__(self, name: str) -> Any:
        if self._models is None:
            from qdrant_client.http import models as _qm
            self._models = _qm
        return getattr(self._models, name)

qdrant_models: Any = _LazyQdrantModels()

from .config import settings
from .local_endpoint_policy import LocalEndpointError
from .local_endpoint_policy import open_local_url
from .local_endpoint_policy import read_bounded_response
from .local_endpoint_policy import validate_local_endpoint
from .project_registry import canonical_project_id
from .resource_limits import QDRANT_SDK_TIMEOUT_SECONDS
from .resource_limits import QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS
from .resource_limits import QDRANT_HEALTH_SUCCESS_CACHE_TTL_SECONDS
from .runtime_storage import MemoryStoreMode
from .runtime_storage import resolve_runtime_storage_mode
from .storage_state import StorageState
from .storage_state import evaluate_storage_state

from .mem0_adapter_decay import (
    DECAY_ARCHIVE_PATH,  # noqa: F401
    DECAY_LAMBDA_BY_MEMORY_TYPE,  # noqa: F401
    DECAY_LAMBDA_BY_SEMANTIC_TYPE,  # noqa: F401
    DECAY_LAMBDA_PER_DAY,  # noqa: F401
    _append_decayed_payload_archive,  # noqa: F401
    _parse_memory_timestamp,  # noqa: F401
    decay_lambda_for_payload,  # noqa: F401
    ensure_decay_metadata,  # noqa: F401
    initial_decay_metadata,  # noqa: F401
    memory_decay_score,  # noqa: F401
    normalize_access_count,  # noqa: F401
    normalize_importance_score,  # noqa: F401
    utc_now_iso,  # noqa: F401
)
from .mem0_adapter_graph import (
    LEXICAL_STOPWORDS,  # noqa: F401
    LEXICAL_TOKEN_RE,  # noqa: F401
    SEMANTIC_EDGE_TYPES,  # noqa: F401
    SEMANTIC_GRAPH_PATH,  # noqa: F401
    BHMGraphManager,  # noqa: F401
    _lexical_tokens,  # noqa: F401
    lexical_score,  # noqa: F401
    normalize_semantic_edge_type,  # noqa: F401
    reciprocal_rank_fusion,  # noqa: F401
)

LOCAL_COLLECTION_PREFIX = "bhm_local_memory"
GLOBAL_COLLECTION_NAME = "bhm_global_core_knowledge"
_QDRANT_UNAVAILABLE_CACHE_TTL_SECONDS = 2.0
_qdrant_health_cache_lock = threading.Lock()
_qdrant_unavailable_until = 0.0
_qdrant_available_until = 0.0
_QDRANT_HEALTH_DIAGNOSTIC_SCHEMA_VERSION = "bhm.qdrant-health-diagnostic.v1"
_qdrant_health_diagnostic: dict[str, Any] = {
    "last_checked_at": None,
    "last_success_at": None,
    "last_failure_at": None,
    "last_failure_class": None,
    "last_http_status": None,
    "last_latency_ms": None,
    "consecutive_failures": 0,
    "total_failures": 0,
    "total_successes": 0,
}
QDRANT_LOCAL_PATH = settings.runtime_dir / "qdrant-local"

class StorageNotReady(RuntimeError):
    """Raised when configured storage policy cannot provide its required backend."""

def _qdrant_health_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _qdrant_health_latency_ms(started_at: float) -> int:
    return max(0, min(int(round((time.monotonic() - started_at) * 1000)), 60_000))

def _qdrant_health_failure_class(error: BaseException) -> tuple[str, int | None]:
    """Classify local probe failures without exposing endpoint or exception text."""

    if isinstance(error, urllib.error.HTTPError):
        return f"http_{max(100, min(int(error.code), 599))}", int(error.code)
    if isinstance(error, LocalEndpointError):
        return "local_policy_rejected", None
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout", None
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout", None
        if isinstance(reason, ConnectionRefusedError):
            return "connection_refused", None
        if isinstance(reason, OSError) and getattr(reason, "errno", None) in {111, 10061}:
            return "connection_refused", None
        return "transport_error", None
    if isinstance(error, ConnectionRefusedError):
        return "connection_refused", None
    if isinstance(error, OSError) and getattr(error, "errno", None) in {111, 10061}:
        return "connection_refused", None
    return "transport_error", None

def _record_qdrant_health_success(*, checked_at: str, latency_ms: int) -> None:
    with _qdrant_health_cache_lock:
        _qdrant_health_diagnostic.update(
            {
                "last_checked_at": checked_at,
                "last_success_at": checked_at,
                "last_failure_class": None,
                "last_http_status": 200,
                "last_latency_ms": latency_ms,
                "consecutive_failures": 0,
                "total_successes": int(_qdrant_health_diagnostic["total_successes"]) + 1,
            }
        )

def _record_qdrant_health_failure(
    *, checked_at: str, failure_class: str, http_status: int | None, latency_ms: int
) -> None:
    with _qdrant_health_cache_lock:
        _qdrant_health_diagnostic.update(
            {
                "last_checked_at": checked_at,
                "last_failure_at": checked_at,
                "last_failure_class": failure_class,
                "last_http_status": http_status,
                "last_latency_ms": latency_ms,
                "consecutive_failures": int(_qdrant_health_diagnostic["consecutive_failures"]) + 1,
                "total_failures": int(_qdrant_health_diagnostic["total_failures"]) + 1,
            }
        )

def qdrant_health_diagnostics() -> dict[str, Any]:
    """Return bounded probe provenance for authenticated runtime health only."""

    now = time.monotonic()
    with _qdrant_health_cache_lock:
        payload = dict(_qdrant_health_diagnostic)
        negative_cache_active = now < _qdrant_unavailable_until
    payload.update(
        {
            "schema_version": _QDRANT_HEALTH_DIAGNOSTIC_SCHEMA_VERSION,
            "negative_cache_active": negative_cache_active,
        }
    )
    return payload

def _reset_qdrant_health_diagnostics_for_tests() -> None:
    """Reset process-local probe state for deterministic unit tests."""

    global _qdrant_unavailable_until, _qdrant_available_until
    with _qdrant_health_cache_lock:
        _qdrant_unavailable_until = 0.0
        _qdrant_available_until = 0.0
        _qdrant_health_diagnostic.update(
            {
                "last_checked_at": None,
                "last_success_at": None,
                "last_failure_at": None,
                "last_failure_class": None,
                "last_http_status": None,
                "last_latency_ms": None,
                "consecutive_failures": 0,
                "total_failures": 0,
                "total_successes": 0,
            }
        )

def _remote_qdrant_available(*, opener: Any = None, timeout: float = QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS) -> bool:
    global _qdrant_unavailable_until, _qdrant_available_until
    now = time.monotonic()
    with _qdrant_health_cache_lock:
        if now < _qdrant_unavailable_until:
            return False
        if now < _qdrant_available_until:
            return True
    transport = open_local_url if opener is None else opener
    started_at = time.monotonic()
    checked_at = _qdrant_health_timestamp()
    try:
        request = urllib.request.Request(
            f"{settings.qdrant_url.rstrip('/')}/healthz",
            headers={"User-Agent": "BlackHoleMemory-Qdrant-Health/1.0"},
        )
        with transport(request, timeout=timeout) as response:
            read_bounded_response(response, limit=128)
            status = int(getattr(response, "status", 200))
            available = 200 <= status < 300
            if available:
                _record_qdrant_health_success(
                    checked_at=checked_at,
                    latency_ms=_qdrant_health_latency_ms(started_at),
                )
            else:
                _record_qdrant_health_failure(
                    checked_at=checked_at,
                    failure_class=f"http_{max(100, min(status, 599))}",
                    http_status=status,
                    latency_ms=_qdrant_health_latency_ms(started_at),
                )
    # broad: health probing must convert any backend failure into a diagnostic state.
    except Exception as error:
        available = False
        failure_class, http_status = _qdrant_health_failure_class(error)
        _record_qdrant_health_failure(
            checked_at=checked_at,
            failure_class=failure_class,
            http_status=http_status,
            latency_ms=_qdrant_health_latency_ms(started_at),
        )
    with _qdrant_health_cache_lock:
        if available:
            _qdrant_available_until = time.monotonic() + QDRANT_HEALTH_SUCCESS_CACHE_TTL_SECONDS
            _qdrant_unavailable_until = 0.0
        else:
            _qdrant_available_until = 0.0
            _qdrant_unavailable_until = time.monotonic() + _QDRANT_UNAVAILABLE_CACHE_TTL_SECONDS
    return available

def _qdrant_connection_config() -> dict[str, Any]:
    state = evaluate_storage_state(None, remote_available=_remote_qdrant_available())
    if state.backend == "remote":
        config: dict[str, Any] = {
            "url": settings.qdrant_url,
            "timeout": QDRANT_SDK_TIMEOUT_SECONDS,
        }
        if settings.qdrant_api_key:
            config["api_key"] = settings.qdrant_api_key
        return config

    if state.backend == "embedded-local":
        QDRANT_LOCAL_PATH.mkdir(parents=True, exist_ok=True)
        return {"path": str(QDRANT_LOCAL_PATH)}
    raise StorageNotReady(state.reason)

def _memory_collection_names(client: Any) -> list[str]:
    collections = client.get_collections().collections
    names: list[str] = []
    for collection in collections:
        name = str(getattr(collection, "name", "") or "")
        if name == GLOBAL_COLLECTION_NAME or name.startswith(f"{LOCAL_COLLECTION_PREFIX}_"):
            names.append(name)
    return names

def _evict_stale_memories_sync(threshold: float = 0.2) -> dict[str, Any]:
    client = get_qdrant_client()
    # Qdrant is a rebuildable projection.  A destructive projection cleanup
    # still requires a read-only confirmation that its source row exists in
    # the canonical SQLite store and belongs to the same project.
    from .memory_repository import SQLiteMemoryRepository

    authoritative = SQLiteMemoryRepository(settings.runtime_dir / "live-memory" / "memories.sqlite3")
    now_dt = datetime.now(timezone.utc)
    archived_at = now_dt.isoformat().replace("+00:00", "Z")
    scanned = 0
    evicted: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    schema_probe = getattr(authoritative, "_schema_is_ready_without_writes", None)
    if callable(schema_probe):
        try:
            schema_ready = bool(schema_probe())
        # broad: readiness probe is an observational boundary and must never abort vacuum.
        except Exception as exc:
            schema_ready = False
            errors.append(
                {
                    "collection": "authoritative-sqlite",
                    "error": f"authoritative readiness probe failed: {type(exc).__name__}",
                }
            )
        if not schema_ready:
            if not errors:
                errors.append(
                    {
                        "collection": "authoritative-sqlite",
                        "error": "authoritative SQLite is not ready",
                    }
                )
            return {
                "ok": False,
                "threshold": threshold,
                "scanned": scanned,
                "evicted_count": 0,
                "evicted": evicted,
                "archive_path": str(DECAY_ARCHIVE_PATH),
                "errors": errors,
            }

    for collection_name in _memory_collection_names(client):
        offset = None
        while True:
            try:
                points, offset = client.scroll(
                    collection_name=collection_name,
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
            # broad: remote projection scan failures are recorded and scanning stops safely.
            except Exception as exc:
                errors.append({"collection": collection_name, "error": str(exc)})
                break

            if not points:
                break

            for point in points:
                scanned += 1
                payload = dict(point.payload or {})
                raw_score = float(payload.get("score") or 1.0)
                score = memory_decay_score(payload, raw_qdrant_score=raw_score, now=now_dt)
                if score >= threshold:
                    continue

                source_id = str(payload.get("source_id") or "").strip()
                project = str(payload.get("project") or "").strip()
                if not source_id or not project:
                    errors.append(
                        {
                            "collection": collection_name,
                            "error": "stale projection has no authoritative source/project scope",
                        }
                    )
                    continue
                try:
                    source_memory = authoritative.get_memory(source_id, project=project)
                # broad: authoritative lookup failures are recorded per item and do not abort the sweep.
                except Exception as exc:
                    errors.append(
                        {
                            "collection": collection_name,
                            "error": f"authoritative lookup failed: {type(exc).__name__}",
                        }
                    )
                    continue
                if source_memory is None:
                    errors.append(
                        {
                            "collection": collection_name,
                            "error": "authoritative memory missing for stale projection",
                        }
                    )
                    continue

                try:
                    current_points = client.retrieve(
                        collection_name=collection_name,
                        ids=[point.id],
                        with_payload=True,
                        with_vectors=False,
                    )
                # broad: projection revalidation failures are recorded per item and do not abort the sweep.
                except Exception as exc:
                    errors.append(
                        {
                            "collection": collection_name,
                            "error": f"stale projection revalidation failed: {type(exc).__name__}",
                        }
                    )
                    continue
                if len(current_points or []) != 1:
                    errors.append(
                        {
                            "collection": collection_name,
                            "error": "stale projection disappeared before delete",
                        }
                    )
                    continue
                current_payload = dict(getattr(current_points[0], "payload", None) or {})
                encoded_before = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                encoded_after = json.dumps(
                    current_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                if hashlib.sha256(encoded_before).digest() != hashlib.sha256(encoded_after).digest():
                    errors.append(
                        {
                            "collection": collection_name,
                            "error": "stale projection changed before delete",
                        }
                    )
                    continue
                if str(current_payload.get("project") or "").strip() != project:
                    errors.append(
                        {
                            "collection": collection_name,
                            "error": "stale projection crossed project boundary",
                        }
                    )
                    continue

                _append_decayed_payload_archive(
                    collection_name=collection_name,
                    point_id=point.id,
                    payload=payload,
                    score=score,
                    threshold=threshold,
                    archived_at=archived_at,
                )
                client.delete(
                    collection_name=collection_name,
                    points_selector=qdrant_models.FilterSelector(
                        filter=qdrant_models.Filter(
                            must=[
                                qdrant_models.FieldCondition(
                                    key="project",
                                    match=qdrant_models.MatchValue(value=project),
                                ),
                                qdrant_models.HasIdCondition(has_id=[point.id]),
                            ]
                        )
                    ),
                    wait=True,
                )
                evicted.append({"collection": collection_name, "point_id": str(point.id), "decay_score": score})

            if offset is None:
                break

    return {
        "ok": not errors,
        "threshold": threshold,
        "scanned": scanned,
        "evicted_count": len(evicted),
        "evicted": evicted,
        "archive_path": str(DECAY_ARCHIVE_PATH),
        "errors": errors,
    }

async def evict_stale_memories(threshold: float = 0.2) -> dict[str, Any]:
    return await asyncio.to_thread(_evict_stale_memories_sync, threshold)

def _project_slug(project_name: str | None = None) -> str:
    raw = canonical_project_id(project_name or settings.qdrant_collection or settings.app_name or "blackholememory")
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return slug or "blackholememory"


def local_collection_name(project_name: str | None = None) -> str:
    return f"{LOCAL_COLLECTION_PREFIX}_{_project_slug(project_name)}"


def global_collection_name() -> str:
    return GLOBAL_COLLECTION_NAME


def storage_runtime_state() -> StorageState:
    return evaluate_storage_state(None, remote_available=_remote_qdrant_available())


def mem0_runtime_plan() -> dict[str, Any]:
    state = storage_runtime_state()
    memory_store_mode = resolve_runtime_storage_mode(environ=os.environ)
    projection_only = memory_store_mode is MemoryStoreMode.SQLITE_AUTHORITATIVE
    return {
        "enabled": settings.mem0_enabled,
        "provider_hint": settings.mem0_provider_hint,
        "integration_mode": "in-process-python-package",
        "storage_mode": state.configured_mode,
        "qdrant_mode": state.backend,
        "storage_readiness": state.readiness,
        "storage_reason": state.reason,
        "storage_degraded": not state.ready,
        "qdrant_health": qdrant_health_diagnostics(),
        "local_collection_prefix": LOCAL_COLLECTION_PREFIX,
        "default_local_collection_name": local_collection_name(settings.qdrant_collection),
        "global_collection_name": GLOBAL_COLLECTION_NAME,
        "embedding_dims": settings.mem0_embedding_dims,
        "status": (
            "projection-only"
            if settings.mem0_enabled and state.ready and projection_only
            else "writer-enabled"
            if settings.mem0_enabled and state.ready
            else "degraded"
            if settings.mem0_enabled
            else "disabled"
        ),
        "memory_store_mode": memory_store_mode.value,
        "direct_vector_writes": not projection_only,
    }


def build_mem0_config(collection_name: str) -> dict[str, Any]:
    # Mem0 validates the Qdrant provider config against its own schema, which
    # does not accept the SDK client's ``timeout`` option. Keep the bounded
    # timeout on the native QdrantClient path and pass only provider-supported
    # fields into Mem0.
    openai_base_url = validate_local_endpoint(settings.mem0_openai_base_url)
    qdrant_config = {
        key: value
        for key, value in _qdrant_connection_config().items()
        if key != "timeout"
    }
    qdrant_config.update(
        {
            "collection_name": collection_name,
            "embedding_model_dims": settings.mem0_embedding_dims,
            "on_disk": True,
        }
    )
    return {
        "vector_store": {
            "provider": "qdrant",
            "config": qdrant_config,
        },
        "llm": {
            "provider": settings.mem0_llm_provider,
            "config": {
                "api_key": settings.mem0_api_key,
                "openai_base_url": openai_base_url,
                "model": settings.mem0_llm_model,
            },
        },
        "embedder": {
            "provider": settings.mem0_embedder_provider,
            "config": {
                "api_key": settings.mem0_api_key,
                "openai_base_url": openai_base_url,
                "model": settings.mem0_embedding_model,
                "embedding_dims": settings.mem0_embedding_dims,
            },
        },
    }


@lru_cache(maxsize=1)
def get_qdrant_client() -> Any:
    from qdrant_client import QdrantClient

    config = dict(_qdrant_connection_config())
    config.setdefault("timeout", QDRANT_SDK_TIMEOUT_SECONDS)
    return QdrantClient(**config)


def _ensure_qdrant_collection(collection_name: str) -> dict[str, Any]:
    client = get_qdrant_client()
    existed = client.collection_exists(collection_name)
    if not existed:
        try:
            client.create_collection(
                collection_name=collection_name,
                vectors_config=qdrant_models.VectorParams(
                    size=settings.mem0_embedding_dims,
                    distance=qdrant_models.Distance.COSINE,
                ),
            )
        # broad: collection creation races with another worker; existence check resolves the boundary.
        except Exception as exc:
            logging.getLogger(__name__).warning("collection creation failed: %s", type(exc).__name__)
            if not client.collection_exists(collection_name):
                raise
            existed = True
    return {
        "collection_name": collection_name,
        "existed": existed,
        "created": not existed,
    }


def ensure_memory_collections(project_name: str | None = None) -> dict[str, Any]:
    local_name = local_collection_name(project_name)
    local = _ensure_qdrant_collection(local_name)
    global_core = _ensure_qdrant_collection(GLOBAL_COLLECTION_NAME)
    return {
        "project": _project_slug(project_name),
        "local": local,
        "global": global_core,
    }


@lru_cache(maxsize=16)
def get_mem0_memory(collection_name: str | None = None, *, ensure_collection: bool = True) -> Any:
    from mem0 import Memory

    resolved_collection_name = collection_name or local_collection_name(settings.qdrant_collection)
    if ensure_collection:
        _ensure_qdrant_collection(resolved_collection_name)
    return Memory.from_config(build_mem0_config(resolved_collection_name))


def get_project_mem0_memory(
    project_name: str | None = None,
    *,
    collection_name: str | None = None,
    ensure_collection: bool = True,
) -> Any:
    return get_mem0_memory(
        collection_name or local_collection_name(project_name),
        ensure_collection=ensure_collection,
    )


def get_global_core_memory() -> Any:
    return get_mem0_memory(GLOBAL_COLLECTION_NAME)


def reset_mem0_collection() -> dict[str, Any]:
    client = get_qdrant_client()
    collection_name = local_collection_name(settings.qdrant_collection)
    existed = client.collection_exists(collection_name)
    if existed:
        client.delete_collection(collection_name)
    get_mem0_memory.cache_clear()
    return {
        "collection_name": collection_name,
        "existed": existed,
        "deleted": existed,
    }
