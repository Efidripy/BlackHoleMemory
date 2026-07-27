"""Bounded, operator-gated readiness for provider-backed semantic search.

The readiness contract is deliberately metadata-only.  It binds one semantic
request to the authoritative SQLite graph/repository epoch, the expected
projection completeness and the already-warmed provider contract.  A missing
or stale receipt is a normal ``not_ready`` result: callers must refresh or
warm the project explicitly and must not start a provider as a side effect.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from typing import Any


SEMANTIC_READINESS_SCHEMA_VERSION = "bhm.semantic-readiness.v1"
DEFAULT_CACHE_TTL_SECONDS = 30.0


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def project_warmup_state(project: str, warmup_status: Mapping[str, Any]) -> tuple[bool, bool | None, str]:
    """Return bounded project warmup state without exposing the project list."""

    enabled = bool(warmup_status.get("memory_warmup_enabled"))
    if not enabled:
        return False, None, "disabled"
    key = str(project or "").strip().casefold()
    warmed = {
        str(item or "").strip().casefold()
        for item in list(warmup_status.get("memory_projects") or [])
        if str(item or "").strip()
    }
    skipped = {
        str(item or "").strip().casefold()
        for item in list(warmup_status.get("memory_skipped_projects") or [])
        if str(item or "").strip()
    }
    if key in warmed:
        return True, True, "warmed"
    if key in skipped:
        return True, False, "skipped"
    return True, False, "unlisted"


def build_readiness_key(
    *,
    project: str,
    graph_snapshot_id: str,
    graph_digest: str,
    repository_snapshot_digest: str,
    parser_registry_digest: str,
    embedding_contract_digest: str,
    source_row_count: int,
    selected_count: int,
    projected_count: int,
    projection_pending: int,
    projection_failed: int,
    provider_ready: bool | None = None,
    project_warmup_enabled: bool = False,
    project_warmup_ready: bool | None = None,
) -> str:
    """Return a deterministic cache key; no source or vectors are included."""

    return _digest(
        {
            "schema_version": SEMANTIC_READINESS_SCHEMA_VERSION,
            "project": str(project or "").casefold(),
            "graph_snapshot_id": str(graph_snapshot_id or ""),
            "graph_digest": str(graph_digest or ""),
            "repository_snapshot_digest": str(repository_snapshot_digest or ""),
            "parser_registry_digest": str(parser_registry_digest or ""),
            "embedding_contract_digest": str(embedding_contract_digest or ""),
            "source_row_count": int(source_row_count),
            "selected_count": int(selected_count),
            "projected_count": int(projected_count),
            "projection_pending": int(projection_pending),
            "projection_failed": int(projection_failed),
            "provider_ready": provider_ready is True,
            "project_warmup_enabled": bool(project_warmup_enabled),
            "project_warmup_ready": project_warmup_ready is True if project_warmup_enabled else None,
        }
    )


class SemanticReadinessCache:
    """Small process-local TTL cache for immutable readiness receipts."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS) -> None:
        self.ttl_seconds = max(float(ttl_seconds), 0.1)
        self._lock = threading.RLock()
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, key: str, *, now: float | None = None) -> dict[str, Any] | None:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            item = self._items.get(str(key or ""))
            if item is None:
                return None
            expires_at, receipt = item
            if expires_at <= current:
                self._items.pop(str(key or ""), None)
                return None
            result = dict(receipt)
            result["cache_hit"] = True
            result["cache_expires_in_seconds"] = round(max(expires_at - current, 0.0), 3)
            return result

    def put(self, key: str, receipt: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
        current = time.monotonic() if now is None else float(now)
        value = dict(receipt)
        value["cache_hit"] = False
        value["cache_ttl_seconds"] = self.ttl_seconds
        with self._lock:
            self._items[str(key or "")] = (current + self.ttl_seconds, dict(value))
        return value

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


def evaluate_semantic_readiness(
    *,
    project: str,
    graph_snapshot_id: str,
    graph_digest: str,
    current_graph_snapshot_id: str,
    graph_repository_snapshot_id: str = "",
    current_repository_snapshot_id: str = "",
    repository_snapshot_digest: str,
    parser_registry_digest: str,
    embedding_contract_digest: str,
    provider_ready: bool | None,
    runtime_slo_status: str,
    source_row_count: int,
    selected_count: int,
    projected_count: int,
    projection_pending: int = 0,
    projection_failed: int = 0,
    skipped_count: int = 0,
    project_warmup_enabled: bool = False,
    project_warmup_ready: bool | None = None,
) -> dict[str, Any]:
    """Evaluate the fail-closed readiness gate without side effects."""

    source_count = max(int(source_row_count), 0)
    selected = max(int(selected_count), 0)
    projected = max(int(projected_count), 0)
    pending = max(int(projection_pending), 0)
    failed = max(int(projection_failed), 0)
    skipped = max(int(skipped_count), 0)
    warmup_enabled = bool(project_warmup_enabled)
    graph_id = str(graph_snapshot_id or "").strip()
    current_id = str(current_graph_snapshot_id or "").strip()
    graph_repo_id = str(graph_repository_snapshot_id or "").strip()
    current_repo_id = str(current_repository_snapshot_id or "").strip()
    graph_hash = str(graph_digest or "").strip()
    repo_hash = str(repository_snapshot_digest or "").strip()
    parser_hash = str(parser_registry_digest or "").strip()
    embedding_hash = str(embedding_contract_digest or "").strip()
    slo = str(runtime_slo_status or "unknown").strip().casefold()

    failures: list[str] = []
    requirements: list[str] = []
    if not graph_id or not graph_hash or not repo_hash:
        failures.append("authoritative_snapshot_identity_missing")
        requirements.append("operator_refresh_index_and_graph")
    if current_id and graph_id != current_id:
        failures.append("graph_snapshot_stale")
        requirements.append("operator_refresh_index_and_graph")
    if graph_repo_id and current_repo_id and graph_repo_id != current_repo_id:
        failures.append("repository_snapshot_stale")
        requirements.append("operator_refresh_index_and_graph")
    if source_count != selected or skipped > 0:
        failures.append("projection_selection_incomplete")
        requirements.append("operator_projection_refresh")
    if projected != selected:
        failures.append("projection_point_completeness_mismatch")
        requirements.append("operator_projection_refresh")
    if pending > 0 or failed > 0:
        failures.append("projection_outbox_not_drained")
        requirements.append("operator_projection_drain")
    if not parser_hash:
        failures.append("parser_contract_missing")
        requirements.append("operator_refresh_index_and_graph")
    if not embedding_hash or provider_ready is not True:
        failures.append("provider_warmup_not_ready")
        requirements.append("operator_provider_warmup")
    if warmup_enabled and project_warmup_ready is not True:
        failures.append("project_provider_warmup_not_ready")
        requirements.append("operator_provider_project_warmup")
    if slo not in {"healthy", "ready", "ok", "pass"}:
        failures.append("runtime_slo_not_healthy")
        requirements.append("operator_restore_runtime_slo")

    projection_required = any(
        item in failures
        for item in (
            "authoritative_snapshot_identity_missing",
            "graph_snapshot_stale",
            "projection_selection_incomplete",
            "projection_point_completeness_mismatch",
            "projection_outbox_not_drained",
            "parser_contract_missing",
        )
    )
    warmup_required = any(
        item in failures
        for item in ("provider_warmup_not_ready", "project_provider_warmup_not_ready")
    )
    ready = not failures
    return {
        "schema_version": SEMANTIC_READINESS_SCHEMA_VERSION,
        "ready": ready,
        "request_status": "ready" if ready else "not_ready",
        "freshness": "fresh" if not any(item in failures for item in ("authoritative_snapshot_identity_missing", "graph_snapshot_stale")) else "stale",
        "requires_operator_projection": projection_required,
        "requires_operator_warmup": warmup_required,
        "requirements": list(dict.fromkeys(requirements)),
        "failures": failures,
        "binding": {
            "project": str(project or "").casefold(),
            "graph_snapshot_id": graph_id,
            "current_graph_snapshot_id": current_id,
            "graph_repository_snapshot_id": graph_repo_id,
            "current_repository_snapshot_id": current_repo_id,
            "graph_digest": graph_hash,
            "repository_snapshot_digest": repo_hash,
            "parser_registry_digest": parser_hash,
            "embedding_contract_digest": embedding_hash,
        },
        "completeness": {
            "source_row_count": source_count,
            "selected_count": selected,
            "projected_count": projected,
            "skipped_count": skipped,
            "projection_pending": pending,
            "projection_failed": failed,
        },
        "provider": {
            "ready": provider_ready is True,
            "observed": provider_ready,
            "project_warmup_enabled": warmup_enabled,
            "project_warmup_ready": project_warmup_ready if warmup_enabled else None,
        },
        "runtime_slo_status": slo,
        "execution": {
            "provider_called": False,
            "model_started": False,
            "network_called": False,
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "raw_source_returned": False,
        },
    }


__all__ = [
    "DEFAULT_CACHE_TTL_SECONDS",
    "SEMANTIC_READINESS_SCHEMA_VERSION",
    "SemanticReadinessCache",
    "build_readiness_key",
    "evaluate_semantic_readiness",
    "project_warmup_state",
]
