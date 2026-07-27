from __future__ import annotations

import threading
import time

from blackholememory.runtime_storage import MemoryStoreMode
from blackholememory.runtime_storage import RuntimeReadiness
from blackholememory.runtime_storage import clear_memory_store_schema_cache
from blackholememory.runtime_storage import evaluate_runtime_storage_state
from blackholememory.runtime_storage import inspect_memory_store_schema
from blackholememory.runtime_storage import resolve_runtime_storage_config
from blackholememory.runtime_storage import resolve_runtime_storage_mode
from blackholememory.runtime_storage import runtime_storage_state
from blackholememory.memory_repository import SQLiteMemoryRepository


def test_memory_store_mode_fails_closed_to_sqlite_authoritative():
    assert resolve_runtime_storage_mode("unknown") is MemoryStoreMode.SQLITE_AUTHORITATIVE
    assert resolve_runtime_storage_mode("shadow") is MemoryStoreMode.SQLITE_SHADOW
    assert resolve_runtime_storage_mode("authoritative") is MemoryStoreMode.SQLITE_AUTHORITATIVE


def test_runtime_config_defaults_to_sqlite_authoritative_and_worker_disabled(tmp_path):
    config = resolve_runtime_storage_config(runtime_dir=tmp_path, environ={})

    assert config.mode is MemoryStoreMode.SQLITE_AUTHORITATIVE
    assert config.database_path == (tmp_path / "live-memory" / "memories.sqlite3").resolve()
    assert config.projection_worker.enabled is False


def test_shadow_state_is_degraded_until_sqlite_target_exists(tmp_path):
    config = resolve_runtime_storage_config(
        runtime_dir=tmp_path,
        environ={"BHM_MEMORY_STORE_MODE": "sqlite-shadow"},
    )

    missing = evaluate_runtime_storage_state(config)
    assert missing.readiness == RuntimeReadiness.DEGRADED.value
    assert missing.reason == "sqlite_shadow_database_missing"

    config.database_path.parent.mkdir(parents=True)
    config.database_path.touch()
    invalid = evaluate_runtime_storage_state(config, database_ready=False)
    assert invalid.readiness == RuntimeReadiness.DEGRADED.value
    assert invalid.reason == "sqlite_shadow_database_invalid"

    SQLiteMemoryRepository(config.database_path).initialize()
    ready = evaluate_runtime_storage_state(config, database_ready=True)
    assert ready.ready is True
    assert ready.backend == "sqlite-shadow"
    assert ready.database_schema_ready is True


def test_authoritative_state_never_claims_ready_without_switch_guard(tmp_path):
    config = resolve_runtime_storage_config(
        runtime_dir=tmp_path,
        environ={"BHM_MEMORY_STORE_MODE": "sqlite-authoritative"},
    )
    config.database_path.parent.mkdir(parents=True)
    config.database_path.touch()

    blocked = evaluate_runtime_storage_state(config)
    assert blocked.readiness == RuntimeReadiness.NOT_READY.value
    assert blocked.reason == "sqlite_authoritative_switch_not_wired"

    guarded = evaluate_runtime_storage_state(
        config,
        parity_ok=True,
        writer_offline_confirmed=True,
        database_ready=True,
        switch_wired=True,
    )
    assert guarded.ready is True
    assert guarded.reason == "sqlite_authoritative_guard_passed"


def test_authoritative_env_markers_are_visible_and_gate_readiness(tmp_path):
    config = resolve_runtime_storage_config(
        runtime_dir=tmp_path,
        environ={
            "BHM_MEMORY_STORE_MODE": "sqlite-authoritative",
            "BHM_MEMORY_STORE_PARITY_CONFIRMED": "true",
            "BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED": "true",
        },
    )
    config.database_path.parent.mkdir(parents=True)
    SQLiteMemoryRepository(config.database_path).initialize()

    state = evaluate_runtime_storage_state(config, database_ready=True, switch_wired=True)

    assert state.ready is True
    assert state.as_dict()["parity_confirmed"] is True
    assert state.as_dict()["writer_offline_confirmed"] is True


def test_runtime_storage_state_inspects_sqlite_schema_without_writing(tmp_path):
    environ = {
        "BHM_MEMORY_STORE_MODE": "sqlite-shadow",
    }
    config = resolve_runtime_storage_config(runtime_dir=tmp_path, environ=environ)
    config.database_path.parent.mkdir(parents=True)
    config.database_path.touch()

    ready, reason = inspect_memory_store_schema(config.database_path)
    assert ready is False
    assert reason == "sqlite_schema_version_invalid"

    blocked = runtime_storage_state(runtime_dir=tmp_path, environ=environ)
    assert blocked.ready is False
    assert blocked.reason == "sqlite_shadow_database_invalid"
    assert blocked.as_dict()["database_schema_ready"] is False

    SQLiteMemoryRepository(config.database_path).initialize()
    ready, reason = inspect_memory_store_schema(config.database_path)
    assert ready is True
    assert reason == "sqlite_schema_valid"
    healthy = runtime_storage_state(runtime_dir=tmp_path, environ=environ)
    assert healthy.ready is True
    assert healthy.as_dict()["database_schema_ready"] is True


def test_memory_store_schema_cache_reuses_recent_full_check(tmp_path, monkeypatch):
    config = resolve_runtime_storage_config(runtime_dir=tmp_path, environ={})
    config.database_path.parent.mkdir(parents=True)
    SQLiteMemoryRepository(config.database_path).initialize()
    clear_memory_store_schema_cache()

    import blackholememory.runtime_storage as runtime_storage

    original_connect = runtime_storage.sqlite3.connect
    calls = 0

    def counting_connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(runtime_storage.sqlite3, "connect", counting_connect)
    assert runtime_storage.inspect_memory_store_schema(config.database_path, cache_ttl_seconds=60) == (
        True,
        "sqlite_schema_valid",
    )
    assert runtime_storage.inspect_memory_store_schema(config.database_path, cache_ttl_seconds=60) == (
        True,
        "sqlite_schema_valid",
    )
    assert calls == 1

    clear_memory_store_schema_cache()


def test_expired_schema_cache_refreshes_in_background(tmp_path, monkeypatch):
    config = resolve_runtime_storage_config(runtime_dir=tmp_path, environ={})
    config.database_path.parent.mkdir(parents=True)
    config.database_path.write_bytes(b"schema-placeholder")
    clear_memory_store_schema_cache()

    import blackholememory.runtime_storage as runtime_storage

    refreshed = threading.Event()
    calls = 0

    def fake_uncached(_path):
        nonlocal calls
        calls += 1
        if calls > 1:
            refreshed.set()
        return True, "sqlite_schema_valid"

    monkeypatch.setattr(runtime_storage, "_inspect_memory_store_schema_uncached", fake_uncached)
    assert runtime_storage.inspect_memory_store_schema(config.database_path, cache_ttl_seconds=0.01) == (
        True,
        "sqlite_schema_valid",
    )
    time.sleep(0.03)
    started = time.perf_counter()
    assert runtime_storage.inspect_memory_store_schema(config.database_path, cache_ttl_seconds=0.01) == (
        True,
        "sqlite_schema_valid",
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 0.1
    assert refreshed.wait(1.0)
    clear_memory_store_schema_cache()

def test_worker_limits_are_bounded_and_invalid_values_fail_closed(tmp_path):
    config = resolve_runtime_storage_config(
        runtime_dir=tmp_path,
        environ={
            "BHM_PROJECTION_WORKER_ENABLED": "yes",
            "BHM_PROJECTION_WORKER_BATCH_SIZE": "not-an-int",
            "BHM_PROJECTION_WORKER_POLL_SECONDS": "0",
            "BHM_PROJECTION_WORKER_MAX_ATTEMPTS": "9999",
        },
    )

    assert config.projection_worker.enabled is True
    assert config.projection_worker.batch_size == 10
    assert config.projection_worker.poll_seconds == 1.0
    assert config.projection_worker.max_attempts == 5
