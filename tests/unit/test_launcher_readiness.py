from __future__ import annotations

import sys
from pathlib import Path


# Resolve the helper beside the launcher, avoiding an unrelated installed
# `scripts` package on developer machines.
# ruff: noqa: E402
SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from bhm_launcher_readiness import start_when_ready
from bhm_launcher_readiness import wait_for_readiness


def test_wait_for_readiness_retries_until_probe_is_ready():
    clock = [0.0]
    states = iter([(False, "warming"), (False, "warming"), (True, "ready")])
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    def probe() -> tuple[bool, str]:
        return next(states)

    result = wait_for_readiness(probe, timeout_seconds=5, poll_seconds=1, monotonic=monotonic, sleep=sleep)

    assert result.ok is True
    assert result.attempts == 3
    assert sleeps == [1.0, 1.0]


def test_start_when_ready_skips_start_when_already_healthy():
    started: list[str] = []
    result = start_when_ready(lambda: started.append("start"), lambda: (True, "already ready"))

    assert result.ok is True
    assert result.started is False
    assert started == []


def test_start_when_ready_rolls_back_after_readiness_timeout(monkeypatch):
    started: list[str] = []
    rolled_back: list[str] = []
    monkeypatch.setattr(
        "bhm_launcher_readiness.wait_for_readiness",
        lambda *_args, **_kwargs: type(
            "Result",
            (),
            {"ok": False, "started": True, "rolled_back": False, "attempts": 2, "elapsed_ms": 10.0, "detail": "timeout"},
        )(),
    )

    result = start_when_ready(
        lambda: started.append("start") or "token",
        lambda: (False, "stopped"),
        rollback=lambda token: rolled_back.append(token),
    )

    assert result.ok is False
    assert result.rolled_back is True
    assert started == ["start"]
    assert rolled_back == ["token"]
