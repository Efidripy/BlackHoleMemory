from __future__ import annotations

import threading
import time

import pytest

from blackholememory.mcp_readiness import ReadinessConfig
from blackholememory.mcp_readiness import ReadinessCoordinator
from blackholememory.mcp_readiness import ReadinessError
from blackholememory.mcp_readiness import default_lock_path


def test_default_lock_path_uses_runtime_boundary_without_localappdata(monkeypatch, tmp_path):
    monkeypatch.delenv("BHM_MCP_STARTUP_LOCK_PATH", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert default_lock_path(tmp_path) == tmp_path / ".runtime" / "mcp" / "mcp-readiness.lock"


def test_connect_only_fails_closed_without_invoking_launcher(tmp_path):
    coordinator = ReadinessCoordinator(
        config=ReadinessConfig(deadline_seconds=0.25, poll_seconds=0.025, lock_path=tmp_path / "startup.lock")
    )
    launches: list[str] = []

    with pytest.raises(ReadinessError) as raised:
        coordinator.ensure_api_ready(
            lambda _timeout: (False, "health/ready unavailable"),
            launcher=lambda _timeout: launches.append("unexpected") or (True, "started"),
        )

    assert raised.value.stage == "api"
    assert raised.value.code == "api_unavailable"
    assert launches == []
    assert coordinator.snapshot()["auto_start"]["requested"] is False
    assert coordinator.snapshot()["api"] == "unavailable"


def test_explicit_auto_start_uses_one_canonical_transaction(tmp_path):
    state = {"ready": False}
    starts: list[float] = []
    coordinator = ReadinessCoordinator(
        config=ReadinessConfig(
            deadline_seconds=1.0,
            poll_seconds=0.025,
            auto_start=True,
            lock_path=tmp_path / "startup.lock",
        )
    )

    def probe(_timeout: float) -> tuple[bool, str]:
        return state["ready"], "health/ready ok" if state["ready"] else "warming"

    def launcher(timeout: float) -> tuple[bool, str]:
        starts.append(timeout)
        state["ready"] = True
        return True, "canonical launcher dispatched"

    assert coordinator.ensure_api_ready(probe, launcher=launcher) is True
    snapshot = coordinator.snapshot()
    assert len(starts) == 1
    assert snapshot["api"] == "ready"
    assert snapshot["auto_start"] == {
        "requested": True,
        "attempted": True,
        "launcher": "canonical:start-bhm-authoritative.ps1",
    }


def test_auto_start_deadline_is_bounded_and_truthful(tmp_path):
    clock = [0.0]
    launches: list[str] = []

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    coordinator = ReadinessCoordinator(
        config=ReadinessConfig(
            deadline_seconds=0.5,
            poll_seconds=0.1,
            auto_start=True,
            lock_path=tmp_path / "startup.lock",
        ),
        monotonic=lambda: clock[0],
        sleeper=sleep,
    )

    with pytest.raises(ReadinessError) as raised:
        coordinator.ensure_api_ready(
            lambda _timeout: (False, "still warming"),
            launcher=lambda _timeout: launches.append("start") or (True, "dispatched"),
        )

    assert raised.value.code == "readiness_deadline_exceeded"
    assert launches == ["start"]
    assert coordinator.snapshot()["api"] == "timeout"
    assert coordinator.snapshot()["deadline"]["expired"] is True


def test_single_flight_lock_prevents_duplicate_start_across_threads(tmp_path):
    state = {"ready": False}
    starts = 0
    starts_lock = threading.Lock()
    config = ReadinessConfig(
        deadline_seconds=2.0,
        poll_seconds=0.025,
        auto_start=True,
        lock_path=tmp_path / "startup.lock",
    )

    def probe(_timeout: float) -> tuple[bool, str]:
        return state["ready"], "ready" if state["ready"] else "warming"

    def launcher(_timeout: float) -> tuple[bool, str]:
        nonlocal starts
        with starts_lock:
            starts += 1
        time.sleep(0.05)
        state["ready"] = True
        return True, "dispatched"

    results: list[bool] = []

    def run() -> None:
        coordinator = ReadinessCoordinator(config=config)
        results.append(coordinator.ensure_api_ready(probe, launcher=launcher))

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert results == [True, True]
    assert starts == 1
