from __future__ import annotations

from blackholememory.storage_state import StorageMode
from blackholememory.storage_state import StorageReadiness
from blackholememory.storage_state import evaluate_storage_state
from blackholememory.storage_state import resolve_storage_mode


def test_storage_mode_resolution_fails_closed_to_remote_required(monkeypatch):
    monkeypatch.delenv("BHM_STORAGE_MODE", raising=False)
    assert resolve_storage_mode() is StorageMode.REMOTE_REQUIRED
    assert resolve_storage_mode("unknown-mode") is StorageMode.REMOTE_REQUIRED
    assert resolve_storage_mode("local") is StorageMode.EMBEDDED_LOCAL


def test_remote_required_is_not_ready_when_qdrant_is_down():
    state = evaluate_storage_state(StorageMode.REMOTE_REQUIRED, remote_available=False)

    assert state.backend == "unavailable"
    assert state.readiness == StorageReadiness.NOT_READY.value
    assert state.ready is False
    assert state.reason == "remote_qdrant_required_but_unavailable"


def test_remote_preferred_fallback_is_explicitly_degraded():
    state = evaluate_storage_state(StorageMode.REMOTE_PREFERRED, remote_available=False)

    assert state.backend == "embedded-local"
    assert state.readiness == StorageReadiness.DEGRADED.value
    assert state.as_dict()["ready"] is False
    assert state.reason == "explicit_remote_preferred_fallback"


def test_embedded_local_mode_is_explicit_and_degraded_even_if_remote_is_up():
    state = evaluate_storage_state(StorageMode.EMBEDDED_LOCAL, remote_available=True)

    assert state.backend == "embedded-local"
    assert state.readiness == StorageReadiness.DEGRADED.value
    assert state.reason == "explicit_embedded_local_mode"

