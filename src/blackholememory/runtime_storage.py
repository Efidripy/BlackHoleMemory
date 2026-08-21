"""Explicit runtime policy for the canonical memory store.

The Qdrant storage state machine answers a different question: whether the
vector contour is available.  This module describes which memory write path is
authoritative and keeps the SQLite cutover fail-closed until the application
routes have actually been switched.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import asdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from typing import Mapping

from .filesystem_boundaries import FilesystemBoundaryError
from .filesystem_boundaries import assert_safe_path
from .resource_limits import SQLITE_READINESS_PROBE_TIMEOUT_SECONDS


RUNTIME_MEMORY_STORE_ENV = "BHM_MEMORY_STORE_MODE"
MEMORY_STORE_PATH_ENV = "BHM_MEMORY_STORE_PATH"
PROJECTION_WORKER_ENABLED_ENV = "BHM_PROJECTION_WORKER_ENABLED"
PROJECTION_WORKER_POLL_SECONDS_ENV = "BHM_PROJECTION_WORKER_POLL_SECONDS"
PROJECTION_WORKER_BATCH_SIZE_ENV = "BHM_PROJECTION_WORKER_BATCH_SIZE"
PROJECTION_WORKER_LEASE_SECONDS_ENV = "BHM_PROJECTION_WORKER_LEASE_SECONDS"
PROJECTION_WORKER_RETRY_AFTER_SECONDS_ENV = "BHM_PROJECTION_WORKER_RETRY_AFTER_SECONDS"
PROJECTION_WORKER_MAX_ATTEMPTS_ENV = "BHM_PROJECTION_WORKER_MAX_ATTEMPTS"
MEMORY_STORE_PARITY_CONFIRMED_ENV = "BHM_MEMORY_STORE_PARITY_CONFIRMED"
MEMORY_STORE_WRITER_OFFLINE_CONFIRMED_ENV = "BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED"

# A full SQLite quick_check is intentionally retained as the integrity proof,
# but repeating it for every health/readiness request makes a live database
# block the API for several seconds on Windows.  Keep a short, process-local
# cache and single-flight the probe. Normal writes change the main database
# signature, so readiness serves the last verified result while a daemon
# refresh checks the new signature. Write and migration paths never use it.
MEMORY_STORE_HEALTH_CACHE_TTL_SECONDS = 15.0
_SCHEMA_CHECK_CACHE: dict[str, tuple[float, tuple[int, int], tuple[bool, str]]] = {}
_SCHEMA_CHECK_LOCK = threading.Lock()
_SCHEMA_CHECK_REFRESHING: set[str] = set()


class MemoryStoreMode(StrEnum):
    """Authoritative memory write path selected for the running process."""

    SQLITE_SHADOW = "sqlite-shadow"
    SQLITE_AUTHORITATIVE = "sqlite-authoritative"


class RuntimeReadiness(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    NOT_READY = "not-ready"


@dataclass(frozen=True)
class ProjectionWorkerConfig:
    """Bounded worker settings; disabled is the safe default."""

    enabled: bool = False
    poll_seconds: float = 1.0
    batch_size: int = 10
    lease_seconds: float = 120.0
    retry_after_seconds: float = 5.0
    max_attempts: int = 5

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeStorageConfig:
    """Resolved paths and policy without performing filesystem writes."""

    mode: MemoryStoreMode
    database_path: Path
    projection_worker: ProjectionWorkerConfig
    parity_confirmed: bool = False
    writer_offline_confirmed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "database_path": str(self.database_path),
            "projection_worker": self.projection_worker.as_dict(),
            "parity_confirmed": self.parity_confirmed,
            "writer_offline_confirmed": self.writer_offline_confirmed,
        }


@dataclass(frozen=True)
class RuntimeStorageState:
    """Observable runtime memory-store state used by health and diagnostics."""

    configured_mode: str
    backend: str
    readiness: str
    reason: str
    database_path: str
    database_exists: bool
    database_schema_ready: bool
    projection_worker: dict[str, Any]
    parity_confirmed: bool
    writer_offline_confirmed: bool

    @property
    def ready(self) -> bool:
        return self.readiness == RuntimeReadiness.READY.value

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"ready": self.ready}


_MODE_ALIASES = {
    "sqlite-shadow": MemoryStoreMode.SQLITE_SHADOW,
    "sqlite_shadow": MemoryStoreMode.SQLITE_SHADOW,
    "shadow": MemoryStoreMode.SQLITE_SHADOW,
    "sqlite-authoritative": MemoryStoreMode.SQLITE_AUTHORITATIVE,
    "sqlite_authoritative": MemoryStoreMode.SQLITE_AUTHORITATIVE,
    "authoritative": MemoryStoreMode.SQLITE_AUTHORITATIVE,
}

_DEFAULT_RUNTIME_DIR = Path(__file__).resolve().parents[2] / ".runtime"
_MEMORY_STORE_SCHEMA_VERSIONS = frozenset({1, 2})
# Compatibility alias for versioned contract/report scripts.  The plural set
# remains the readiness source of truth; this scalar names the latest schema.
_MEMORY_STORE_SCHEMA_VERSION = max(_MEMORY_STORE_SCHEMA_VERSIONS)
_FRESHNESS_SCHEMA_TABLES = frozenset(
    {"freshness_candidates", "freshness_candidate_events", "freshness_scan_state"}
)
_REQUIRED_MEMORY_STORE_TABLES = frozenset(
    {
        "memory_store_meta",
        "memory_revisions",
        "memories",
        "memory_artifacts",
        "memory_links",
        "memory_outbox",
    }
)


def clear_memory_store_schema_cache() -> None:
    """Clear the bounded readiness cache after an operator-side DB change."""

    with _SCHEMA_CHECK_LOCK:
        _SCHEMA_CHECK_CACHE.clear()
        _SCHEMA_CHECK_REFRESHING.clear()


def _inspect_memory_store_schema_uncached(
    database_path: Path,
) -> tuple[bool, str]:
    try:
        database_path = assert_safe_path(database_path)
    except FilesystemBoundaryError:
        return False, "sqlite_schema_unreadable"
    uri = f"file:{database_path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=SQLITE_READINESS_PROBE_TIMEOUT_SECONDS)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in _MEMORY_STORE_SCHEMA_VERSIONS:
                return False, "sqlite_schema_version_invalid"
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not _REQUIRED_MEMORY_STORE_TABLES.issubset(tables):
                return False, "sqlite_schema_tables_missing"
            if version == 2 and not _FRESHNESS_SCHEMA_TABLES.issubset(tables):
                return False, "sqlite_freshness_schema_missing_tables"
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold()
            return (
                (True, "sqlite_schema_valid")
                if quick_check == "ok"
                else (False, "sqlite_integrity_check_failed")
            )
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return False, "sqlite_schema_unreadable"


def _refresh_memory_store_schema_cache(
    database_path: Path,
    key: str,
    signature: tuple[int, int],
) -> None:
    try:
        result = _inspect_memory_store_schema_uncached(database_path)
        try:
            stat = database_path.stat()
            current_signature = (int(stat.st_mtime_ns), int(stat.st_size))
        except OSError:
            current_signature = signature
        with _SCHEMA_CHECK_LOCK:
            _SCHEMA_CHECK_CACHE[key] = (time.monotonic(), current_signature, result)
    finally:
        with _SCHEMA_CHECK_LOCK:
            _SCHEMA_CHECK_REFRESHING.discard(key)


def inspect_memory_store_schema(
    path: Path | str,
    *,
    cache_ttl_seconds: float | None = None,
) -> tuple[bool, str]:
    """Inspect the SQLite memory target without creating or mutating it.

    Readiness callers share a short cache because ``PRAGMA quick_check`` is a
    full-database scan.  The cache is keyed by the main database mtime/size,
    single-flight under a process-local lock, and can be disabled with
    ``cache_ttl_seconds=0`` for tests or explicit forensic checks.
    """

    try:
        raw_path = Path(path).expanduser()
        assert_safe_path(raw_path)
        database_path = raw_path.resolve()
        assert_safe_path(database_path)
    except FilesystemBoundaryError:
        return False, "sqlite_schema_unreadable"
    key = str(database_path)
    ttl = MEMORY_STORE_HEALTH_CACHE_TTL_SECONDS if cache_ttl_seconds is None else max(0.0, float(cache_ttl_seconds))
    if not database_path.exists():
        with _SCHEMA_CHECK_LOCK:
            _SCHEMA_CHECK_CACHE.pop(key, None)
        return False, "sqlite_database_missing"

    try:
        stat = database_path.stat()
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return False, "sqlite_schema_unreadable"

    now = time.monotonic()
    with _SCHEMA_CHECK_LOCK:
        cached = _SCHEMA_CHECK_CACHE.get(key)
        if cached is not None and ttl > 0 and cached[2][0] is True:
            checked_at, cached_signature, cached_result = cached
            refresh_required = cached_signature != signature or now - checked_at >= ttl
            if refresh_required:
                # Serve the last verified result while one daemon refresh runs.
                # A SQLite commit changes mtime/size on every normal write; a
                # synchronous quick_check here would make readiness block behind
                # the write it is supposed to observe.
                if key not in _SCHEMA_CHECK_REFRESHING:
                    _SCHEMA_CHECK_REFRESHING.add(key)
                    threading.Thread(
                        target=_refresh_memory_store_schema_cache,
                        args=(database_path, key, signature),
                        daemon=True,
                        name="bhm-sqlite-health-refresh",
                    ).start()
            return cached_result

        # The first probe and recovery from a cached invalid result remain
        # synchronous and fail-closed. Holding the lock single-flights the
        # expensive quick_check for concurrent readiness requests.
        result = _inspect_memory_store_schema_uncached(database_path)
        if ttl > 0:
            _SCHEMA_CHECK_CACHE[key] = (time.monotonic(), signature, result)
        else:
            _SCHEMA_CHECK_CACHE.pop(key, None)
        return result


def _env_value(name: str, environ: Mapping[str, str] | None) -> str | None:
    if environ is not None:
        return environ.get(name)
    return os.getenv(name)


def _bounded_float(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
    environ: Mapping[str, str] | None,
) -> float:
    raw = _env_value(name, environ)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < minimum or value > maximum:
        return default
    return value


def _bounded_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
    environ: Mapping[str, str] | None,
) -> int:
    raw = _env_value(name, environ)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < minimum or value > maximum:
        return default
    return value


def _env_bool(name: str, default: bool, environ: Mapping[str, str] | None) -> bool:
    raw = _env_value(name, environ)
    if raw is None:
        return default
    normalized = str(raw).strip().casefold()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _resolve_path(raw: str | None, default: Path, runtime_dir: Path) -> Path:
    if raw is None or not str(raw).strip():
        return assert_safe_path(default)
    candidate = Path(str(raw).strip()).expanduser()
    if not candidate.is_absolute():
        candidate = runtime_dir / candidate
    return assert_safe_path(candidate)


def resolve_runtime_storage_mode(
    value: str | MemoryStoreMode | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> MemoryStoreMode:
    """Resolve the memory-store mode; invalid values fail closed to SQLite authoritative."""

    if isinstance(value, MemoryStoreMode):
        return value
    raw = _env_value(RUNTIME_MEMORY_STORE_ENV, environ) if value is None else value
    return _MODE_ALIASES.get(str(raw or "").strip().casefold(), MemoryStoreMode.SQLITE_AUTHORITATIVE)


def resolve_runtime_storage_config(
    *,
    runtime_dir: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeStorageConfig:
    """Resolve runtime paths and bounded worker settings without writing files."""

    root = assert_safe_path(
        Path(runtime_dir or _DEFAULT_RUNTIME_DIR).expanduser(),
        reject_hardlink_target=False,
    )
    database_default = root / "live-memory" / "memories.sqlite3"
    worker = ProjectionWorkerConfig(
        enabled=_env_bool(PROJECTION_WORKER_ENABLED_ENV, False, environ),
        poll_seconds=_bounded_float(
            PROJECTION_WORKER_POLL_SECONDS_ENV, 1.0, 0.01, 300.0, environ
        ),
        batch_size=_bounded_int(
            PROJECTION_WORKER_BATCH_SIZE_ENV, 10, 1, 1_000, environ
        ),
        lease_seconds=_bounded_float(
            PROJECTION_WORKER_LEASE_SECONDS_ENV, 120.0, 1.0, 86_400.0, environ
        ),
        retry_after_seconds=_bounded_float(
            PROJECTION_WORKER_RETRY_AFTER_SECONDS_ENV, 5.0, 0.0, 86_400.0, environ
        ),
        max_attempts=_bounded_int(
            PROJECTION_WORKER_MAX_ATTEMPTS_ENV, 5, 1, 100, environ
        ),
    )
    return RuntimeStorageConfig(
        mode=resolve_runtime_storage_mode(environ=environ),
        database_path=_resolve_path(
            _env_value(MEMORY_STORE_PATH_ENV, environ), database_default, root
        ),
        projection_worker=worker,
        parity_confirmed=_env_bool(MEMORY_STORE_PARITY_CONFIRMED_ENV, False, environ),
        writer_offline_confirmed=_env_bool(
            MEMORY_STORE_WRITER_OFFLINE_CONFIRMED_ENV, False, environ
        ),
    )


def evaluate_runtime_storage_state(
    config: RuntimeStorageConfig,
    *,
    listener_open: bool = False,
    parity_ok: bool | None = None,
    writer_offline_confirmed: bool | None = None,
    database_ready: bool | None = None,
    switch_wired: bool = False,
) -> RuntimeStorageState:
    """Evaluate readiness without creating a database or changing live files.

    ``sqlite-authoritative`` intentionally remains not-ready until the caller
    proves that all memory routes use SQLite and the offline-writer guard has
    passed.  This prevents an environment toggle from creating split-brain
    writes.
    """

    database_exists = config.database_path.exists()
    database_schema_ready = database_exists if database_ready is None else database_ready
    parity_verified = config.parity_confirmed if parity_ok is None else parity_ok
    writer_offline = (
        config.writer_offline_confirmed
        if writer_offline_confirmed is None
        else writer_offline_confirmed
    )
    mode = config.mode
    if mode is MemoryStoreMode.SQLITE_SHADOW:
        if not database_exists:
            readiness = RuntimeReadiness.DEGRADED
            reason = "sqlite_shadow_database_missing"
        elif not database_schema_ready:
            readiness = RuntimeReadiness.DEGRADED
            reason = "sqlite_shadow_database_invalid"
        else:
            readiness = RuntimeReadiness.READY
            reason = "sqlite_shadow_ready"
        return RuntimeStorageState(
            configured_mode=mode.value,
            backend="sqlite-shadow",
            readiness=readiness.value,
            reason=reason,
            database_path=str(config.database_path),
            database_exists=database_exists,
            database_schema_ready=database_schema_ready,
            projection_worker=config.projection_worker.as_dict(),
            parity_confirmed=parity_verified,
            writer_offline_confirmed=writer_offline,
        )

    if not switch_wired:
        reason = "sqlite_authoritative_switch_not_wired"
    elif not database_exists:
        reason = "sqlite_authoritative_database_missing"
    elif not database_schema_ready:
        reason = "sqlite_authoritative_database_invalid"
    elif listener_open:
        reason = "sqlite_authoritative_writer_still_online"
    elif not writer_offline:
        reason = "sqlite_authoritative_writer_gate_not_confirmed"
    elif parity_verified is not True:
        reason = "sqlite_authoritative_parity_not_confirmed"
    else:
        return RuntimeStorageState(
            configured_mode=mode.value,
            backend="sqlite-authoritative",
            readiness=RuntimeReadiness.READY.value,
            reason="sqlite_authoritative_guard_passed",
            database_path=str(config.database_path),
            database_exists=database_exists,
            database_schema_ready=database_schema_ready,
            projection_worker=config.projection_worker.as_dict(),
            parity_confirmed=parity_verified,
            writer_offline_confirmed=writer_offline,
        )
    return RuntimeStorageState(
        configured_mode=mode.value,
        backend="sqlite-authoritative",
        readiness=RuntimeReadiness.NOT_READY.value,
        reason=reason,
        database_path=str(config.database_path),
        database_exists=database_exists,
        database_schema_ready=database_schema_ready,
        projection_worker=config.projection_worker.as_dict(),
        parity_confirmed=parity_verified,
        writer_offline_confirmed=writer_offline,
    )


def runtime_storage_state(
    runtime_dir: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    listener_open: bool = False,
    parity_ok: bool | None = None,
    writer_offline_confirmed: bool | None = None,
    switch_wired: bool = False,
) -> RuntimeStorageState:
    """Return the current policy/readiness snapshot for health surfaces."""

    config = resolve_runtime_storage_config(runtime_dir=runtime_dir, environ=environ)
    database_ready = None
    database_ready, _ = inspect_memory_store_schema(config.database_path)
    return evaluate_runtime_storage_state(
        config,
        listener_open=listener_open,
        parity_ok=parity_ok,
        writer_offline_confirmed=writer_offline_confirmed,
        database_ready=database_ready,
        switch_wired=switch_wired,
    )
