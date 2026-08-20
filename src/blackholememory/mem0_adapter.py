from __future__ import annotations

# Third-party imports intentionally follow local telemetry/logging setup.
# ruff: noqa: E402

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import urllib.request
import warnings
from collections import Counter
from datetime import datetime
from datetime import timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

os.environ.setdefault("MEM0_TELEMETRY", "False")
logging.getLogger("posthog").setLevel(logging.CRITICAL + 1)
logging.getLogger("mem0.utils.spacy_models").setLevel(logging.ERROR)
logging.getLogger("mem0.vector_stores.qdrant").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message="Api key is used with an insecure connection.*")

from mem0 import Memory
from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models

from .config import settings
from .filesystem_boundaries import append_bytes_safely
from .filesystem_boundaries import replace_bytes_safely
from .local_endpoint_policy import open_local_url
from .local_endpoint_policy import read_bounded_response
from .local_endpoint_policy import validate_local_endpoint
from .project_registry import canonical_project_id
from .retrieval_fusion import weighted_rank_fusion
from .resource_limits import QDRANT_SDK_TIMEOUT_SECONDS
from .resource_limits import QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS
from .runtime_storage import MemoryStoreMode
from .runtime_storage import resolve_runtime_storage_mode
from .storage_state import StorageState
from .storage_state import evaluate_storage_state


LOCAL_COLLECTION_PREFIX = "bhm_local_memory"
GLOBAL_COLLECTION_NAME = "bhm_global_core_knowledge"
_QDRANT_UNAVAILABLE_CACHE_TTL_SECONDS = 2.0
_qdrant_health_cache_lock = threading.Lock()
_qdrant_unavailable_until = 0.0
DECAY_ARCHIVE_PATH = settings.runtime_dir / "archive" / "decayed_memory_vault.json"
SEMANTIC_GRAPH_PATH = settings.runtime_dir / "memory" / "semantic_graph.json"
QDRANT_LOCAL_PATH = settings.runtime_dir / "qdrant-local"
DECAY_LAMBDA_PER_DAY = 0.05
DECAY_LAMBDA_BY_SEMANTIC_TYPE = {
    "architecture": 0.025,
    "knowledge": 0.03,
    "decision-log": 0.025,
    "requirement": 0.05,
    "fact": 0.04,
    "bugfix": 0.08,
    "feature": 0.07,
    "refactor": 0.06,
    "log": 0.12,
    "error": 0.12,
}
DECAY_LAMBDA_BY_MEMORY_TYPE = {
    "architecture": 0.025,
    "knowledge": 0.03,
    "knowledge-crystal": 0.025,
    "fact-crystal": 0.025,
    "pattern": 0.025,
    "workflow": 0.08,
    "observation": 0.12,
    "bug": 0.08,
    "bugfix": 0.08,
    "feature": 0.07,
    "refactor": 0.06,
    "log": 0.12,
    "error": 0.12,
}
SEMANTIC_EDGE_TYPES = frozenset({"DEPENDS_ON", "UPGRADES", "CONTRADICTS"})


class StorageNotReady(RuntimeError):
    """Raised when configured storage policy cannot provide its required backend."""


LEXICAL_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
LEXICAL_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)


def _lexical_tokens(value: str) -> list[str]:
    return [
        token.strip("_").casefold()
        for token in LEXICAL_TOKEN_RE.findall(str(value or ""))
        if token.strip("_")
    ]


def lexical_score(query: str, text: str) -> float:
    query_tokens = [token for token in _lexical_tokens(query) if token not in LEXICAL_STOPWORDS]
    text_tokens = _lexical_tokens(text)
    if not query_tokens or not text_tokens:
        return 0.0

    query_counts = Counter(query_tokens)
    text_counts = Counter(text_tokens)
    score = 0.0
    matched = 0
    for token, query_count in query_counts.items():
        occurrences = text_counts.get(token, 0)
        if occurrences <= 0:
            continue
        exact_hits = min(query_count, occurrences)
        matched += exact_hits
        specificity = 1.0 + min(len(token), 24) / 24.0
        frequency_penalty = 1.0 / math.sqrt(occurrences)
        score += exact_hits * specificity * frequency_penalty

    if matched <= 0:
        return 0.0

    coverage = matched / max(sum(query_counts.values()), 1)
    normalized_query = " ".join(query_tokens)
    normalized_text = " ".join(text_tokens)
    phrase_bonus = (1.0 + coverage) if normalized_query and normalized_query in normalized_text else 0.0
    length_norm = math.log2(len(text_tokens) + 2)
    return float((score + coverage + phrase_bonus) / max(length_norm, 1.0))


def reciprocal_rank_fusion(
    semantic_ranks: dict[str, int],
    lexical_ranks: dict[str, int],
    k: int = 60,
) -> dict[str, float]:
    return weighted_rank_fusion(
        {"semantic": semantic_ranks, "lexical": lexical_ranks},
        k=k,
    )


def normalize_semantic_edge_type(edge_type: Any) -> str:
    normalized = str(edge_type or "").strip().upper()
    if normalized not in SEMANTIC_EDGE_TYPES:
        raise ValueError(f"unsupported semantic graph edge_type: {edge_type}")
    return normalized


def _remote_qdrant_available() -> bool:
    global _qdrant_unavailable_until
    now = time.monotonic()
    with _qdrant_health_cache_lock:
        if now < _qdrant_unavailable_until:
            return False
    try:
        request = urllib.request.Request(
            f"{settings.qdrant_url.rstrip('/')}/healthz",
            headers={"User-Agent": "BlackHoleMemory-Qdrant-Health/1.0"},
        )
        with open_local_url(request, timeout=QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS) as response:
            read_bounded_response(response, limit=128)
            available = 200 <= getattr(response, "status", 200) < 300
    except Exception:
        available = False
    if not available:
        with _qdrant_health_cache_lock:
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


class BHMGraphManager:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or SEMANTIC_GRAPH_PATH)
        self._lock = threading.RLock()

    def _read_graph_sync(self) -> dict[str, list[dict[str, str]]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}

        graph: dict[str, list[dict[str, str]]] = {}
        for source_id, links in raw.items():
            source = str(source_id or "").strip()
            if not source or not isinstance(links, list):
                continue
            normalized_links: list[dict[str, str]] = []
            for link in links:
                if not isinstance(link, dict):
                    continue
                target_id = str(link.get("target_id") or "").strip()
                if not target_id:
                    continue
                try:
                    edge_type = normalize_semantic_edge_type(link.get("edge_type"))
                except ValueError:
                    continue
                edge = {"target_id": target_id, "edge_type": edge_type}
                if edge not in normalized_links:
                    normalized_links.append(edge)
            graph[source] = normalized_links
        return graph

    def _write_graph_sync(self, graph: dict[str, list[dict[str, str]]]) -> None:
        payload = (json.dumps(graph, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        retry_delays = (0.025, 0.05, 0.1, 0.2, 0.4, 0.8, None)
        for retry_delay in retry_delays:
            try:
                replace_bytes_safely(self.path, payload)
                return
            except PermissionError:
                if retry_delay is None:
                    raise
                time.sleep(retry_delay)
            except OSError as exc:
                if retry_delay is None or getattr(exc, "winerror", None) not in {5, 32}:
                    raise
                time.sleep(retry_delay)

    def _add_semantic_link_sync(self, source_id: str, target_id: str, edge_type: str) -> dict[str, str]:
        source = str(source_id or "").strip()
        target = str(target_id or "").strip()
        if not source:
            raise ValueError("source_id must not be empty")
        if not target:
            raise ValueError("target_id must not be empty")
        if source == target:
            raise ValueError("source_id and target_id must differ")
        edge = {"target_id": target, "edge_type": normalize_semantic_edge_type(edge_type)}

        with self._lock:
            graph = self._read_graph_sync()
            links = graph.setdefault(source, [])
            if edge not in links:
                links.append(edge)
                self._write_graph_sync(graph)
        return edge

    def _get_linked_nodes_sync(self, node_id: str, edge_types: list[str] | None = None) -> list[dict[str, str]]:
        node = str(node_id or "").strip()
        if not node:
            return []
        allowed: set[str] | None = None
        if edge_types is not None:
            allowed = set()
            for edge_type in edge_types:
                try:
                    allowed.add(normalize_semantic_edge_type(edge_type))
                except ValueError:
                    continue

        with self._lock:
            graph = self._read_graph_sync()
        links = graph.get(node, [])
        if allowed is not None:
            links = [link for link in links if link.get("edge_type") in allowed]
        return [dict(link) for link in links]

    def _get_graph_sync(self) -> dict[str, list[dict[str, str]]]:
        with self._lock:
            graph = self._read_graph_sync()
        return {
            source_id: [dict(link) for link in links]
            for source_id, links in graph.items()
        }

    async def add_semantic_link(self, source_id: str, target_id: str, edge_type: str) -> dict[str, str]:
        return await asyncio.to_thread(self._add_semantic_link_sync, source_id, target_id, edge_type)

    async def get_linked_nodes(self, node_id: str, edge_types: list[str] | None = None) -> list[dict[str, str]]:
        return await asyncio.to_thread(self._get_linked_nodes_sync, node_id, edge_types)

    async def get_graph(self) -> dict[str, list[dict[str, str]]]:
        return await asyncio.to_thread(self._get_graph_sync)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_memory_timestamp(value: Any, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text) if text else None
        except ValueError:
            parsed = None
    if parsed is None:
        parsed = default or datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_importance_score(value: Any, default: int = 5) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = default
    return max(1, min(score, 10))


def normalize_access_count(value: Any, default: int = 1) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = default
    return max(count, 1)


def decay_lambda_for_payload(payload: dict[str, Any]) -> float:
    """Return a bounded retention rate using explicit taxonomy first."""

    nested_metadata = payload.get("metadata")
    metadata = nested_metadata if isinstance(nested_metadata, dict) else {}
    override = payload.get("decay_lambda_per_day") or metadata.get("decay_lambda_per_day")
    try:
        if override is not None:
            return max(0.001, min(float(override), 1.0))
    except (TypeError, ValueError):
        pass

    semantic_type = str(payload.get("semantic_type") or metadata.get("semantic_type") or "").strip().casefold()
    if semantic_type in DECAY_LAMBDA_BY_SEMANTIC_TYPE:
        return DECAY_LAMBDA_BY_SEMANTIC_TYPE[semantic_type]

    memory_type = str(
        payload.get("memory_type") or payload.get("type") or metadata.get("memory_type") or metadata.get("type") or ""
    ).strip().casefold()
    if memory_type in DECAY_LAMBDA_BY_MEMORY_TYPE:
        return DECAY_LAMBDA_BY_MEMORY_TYPE[memory_type]

    scope = str(payload.get("scope") or metadata.get("scope") or "").strip().casefold()
    if scope == "global":
        return 0.03
    return DECAY_LAMBDA_PER_DAY


def memory_decay_score(payload: dict[str, Any], raw_qdrant_score: float = 1.0, now: datetime | None = None) -> float:
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_dt = now_dt.astimezone(timezone.utc)

    last_accessed = _parse_memory_timestamp(
        payload.get("last_accessed_at") or payload.get("updated_at") or payload.get("created_at"),
        default=now_dt,
    )
    delta_days = max((now_dt - last_accessed).days, 0)
    retention = math.exp(-decay_lambda_for_payload(payload) * delta_days)
    importance = normalize_importance_score(payload.get("importance_score"))
    access_count = normalize_access_count(payload.get("access_count"))
    return float(raw_qdrant_score) * (importance / 5.0) * retention * (1.0 + 0.1 * math.log(access_count))


def initial_decay_metadata(metadata: dict[str, Any] | None = None, *, created_at: str | None = None) -> dict[str, Any]:
    payload = dict(metadata or {})
    now = created_at or utc_now_iso()
    payload["importance_score"] = normalize_importance_score(payload.get("importance_score"))
    payload["access_count"] = 1
    payload["last_accessed_at"] = now
    return payload


def ensure_decay_metadata(metadata: dict[str, Any] | None = None, *, fallback_at: str | None = None) -> dict[str, Any]:
    payload = dict(metadata or {})
    now = fallback_at or utc_now_iso()
    payload["importance_score"] = normalize_importance_score(payload.get("importance_score"))
    payload["access_count"] = normalize_access_count(payload.get("access_count"))
    payload["last_accessed_at"] = str(payload.get("last_accessed_at") or now)
    return payload


def _memory_collection_names(client: QdrantClient) -> list[str]:
    collections = client.get_collections().collections
    names: list[str] = []
    for collection in collections:
        name = str(getattr(collection, "name", "") or "")
        if name == GLOBAL_COLLECTION_NAME or name.startswith(f"{LOCAL_COLLECTION_PREFIX}_"):
            names.append(name)
    return names


def _append_decayed_payload_archive(
    *,
    collection_name: str,
    point_id: Any,
    payload: dict[str, Any],
    score: float,
    threshold: float,
    archived_at: str,
) -> None:
    archive_record = {
        "archived_at": archived_at,
        "collection": collection_name,
        "point_id": str(point_id),
        "decay_score": score,
        "threshold": threshold,
        "payload": payload,
    }
    append_bytes_safely(
        DECAY_ARCHIVE_PATH,
        (json.dumps(archive_record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
    )


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
def get_qdrant_client() -> QdrantClient:
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
        except Exception:
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


@lru_cache(maxsize=None)
def get_mem0_memory(collection_name: str | None = None) -> Memory:
    resolved_collection_name = collection_name or local_collection_name(settings.qdrant_collection)
    _ensure_qdrant_collection(resolved_collection_name)
    return Memory.from_config(build_mem0_config(resolved_collection_name))


def get_project_mem0_memory(project_name: str | None = None) -> Memory:
    return get_mem0_memory(local_collection_name(project_name))


def get_global_core_memory() -> Memory:
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
