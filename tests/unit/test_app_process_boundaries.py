from types import SimpleNamespace

import pytest

from blackholememory import app as bhm_app
from blackholememory import app_lifecycle
from blackholememory.resource_limits import (
    PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS,
    PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS,
)


def test_app_pid_inspection_uses_registry_process_bound(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(*_args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return SimpleNamespace(stdout='"123"', returncode=0)

    monkeypatch.setattr(bhm_app.os, "name", "nt")
    monkeypatch.setattr(bhm_app.subprocess, "run", fake_run)

    assert app_lifecycle._is_pid_running(123) is True
    assert captured["timeout"] == PROCESS_EXECUTION_PID_INSPECTION_TIMEOUT_SECONDS


def test_app_termination_grace_clamps_and_rejects_non_finite() -> None:
    assert (
        app_lifecycle._bounded_process_termination_grace_seconds(999)
        == PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS
    )
    with pytest.raises(ValueError, match="finite"):
        app_lifecycle._bounded_process_termination_grace_seconds(float("inf"))
