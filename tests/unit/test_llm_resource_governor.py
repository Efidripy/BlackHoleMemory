from __future__ import annotations

from datetime import datetime, timezone

import pytest

from blackholememory.llm_resource_governor import AdmissionRequest
from blackholememory.llm_resource_governor import GovernorConfig
from blackholememory.llm_resource_governor import LLMResourceGovernor
from blackholememory.llm_resource_governor import MaintenanceWindow
from blackholememory.llm_resource_governor import ResourceSnapshot
from blackholememory.llm_resource_governor import WorkloadLimits


GOOD_GPU = ResourceSnapshot(True, vram_used_mib=4_000, vram_total_mib=12_282, temperature_c=60.0)
OPEN_WINDOW = MaintenanceWindow.parse("00:00-06:00")


def _config(**overrides) -> GovernorConfig:
    values = {
        "max_concurrency": 3,
        "interactive_reserve": 1,
        "maintenance_window": OPEN_WINDOW,
        "limits": {
            "interactive": WorkloadLimits(30, 100),
            "foreground": WorkloadLimits(60, 200),
            "background": WorkloadLimits(90, 300),
        },
    }
    values.update(overrides)
    return GovernorConfig(**values)


def _request(job_id: str, workload: str, *, wall: float = 10, output: int = 10) -> AdmissionRequest:
    return AdmissionRequest(job_id, workload, wall, output)


def test_workload_priority_and_interactive_reserved_capacity():
    governor = LLMResourceGovernor(_config())
    now = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)

    first = governor.admit(_request("background-1", "background"), now=now, resources=GOOD_GPU)
    second = governor.admit(_request("foreground-1", "foreground"), now=now, resources=GOOD_GPU)
    third = governor.admit(_request("interactive-1", "interactive"), now=now, resources=GOOD_GPU)
    blocked = governor.admit(_request("foreground-2", "foreground"), now=now, resources=GOOD_GPU)

    assert first.allowed and second.allowed and third.allowed
    assert third.priority_rank == 0
    assert blocked.allowed is False
    assert blocked.code == "interactive_reserve"


def test_background_requires_window_and_user_activity_pauses_it():
    governor = LLMResourceGovernor(_config())
    closed = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    denied = governor.admit(_request("background-closed", "background"), now=closed, resources=GOOD_GPU)
    assert denied.code == "maintenance_window_closed"

    open_now = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)
    governor.set_user_activity(True)
    denied_for_activity = governor.admit(
        _request("background-active", "background"), now=open_now, resources=GOOD_GPU
    )
    assert denied_for_activity.code == "user_activity_pause"
    foreground = governor.admit(_request("foreground-active", "foreground"), now=open_now, resources=GOOD_GPU)
    assert foreground.allowed is True


def test_gpu_thresholds_fail_closed():
    governor = LLMResourceGovernor(_config(max_temperature_c=80, max_vram_used_ratio=0.8))
    hot = ResourceSnapshot(True, vram_used_mib=4_000, vram_total_mib=12_282, temperature_c=80.0)
    hot_decision = governor.admit(_request("hot", "interactive"), resources=hot)
    assert hot_decision.code == "temperature_threshold"

    full = ResourceSnapshot(True, vram_used_mib=10_000, vram_total_mib=12_282, temperature_c=60.0)
    full_decision = governor.admit(_request("full", "interactive"), resources=full)
    assert full_decision.code == "vram_threshold"

    unavailable = governor.admit(
        _request("unknown-gpu", "interactive"),
        resources=ResourceSnapshot(False),
    )
    assert unavailable.code == "gpu_probe_unavailable"


def test_wall_and_output_limits_are_not_silently_clamped():
    governor = LLMResourceGovernor(_config())
    now = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)
    wall = governor.admit(_request("wall", "foreground", wall=61), now=now, resources=GOOD_GPU)
    output = governor.admit(_request("output", "foreground", output=201), now=now, resources=GOOD_GPU)
    assert wall.code == "wall_limit_exceeded"
    assert output.code == "output_limit_exceeded"


def test_release_and_duplicate_admission_are_idempotent():
    governor = LLMResourceGovernor(_config())
    request = _request("same", "interactive")
    first = governor.admit(request, resources=GOOD_GPU)
    duplicate = governor.admit(request, resources=GOOD_GPU)
    assert first.code == "admitted"
    assert duplicate.code == "already_admitted"
    assert governor.release("same") is True
    assert governor.release("same") is False


def test_env_config_and_cross_midnight_window(monkeypatch):
    monkeypatch.setenv("BHM_LLM_GOVERNOR_MAX_CONCURRENCY", "4")
    monkeypatch.setenv("BHM_LLM_GOVERNOR_INTERACTIVE_RESERVE", "2")
    monkeypatch.setenv("BHM_LLM_GOVERNOR_MAINTENANCE_WINDOW", "23:00-02:00")
    config = GovernorConfig.from_env()
    window = config.maintenance_window
    assert config.max_concurrency == 4
    assert config.interactive_reserve == 2
    assert window is not None
    assert window.contains(datetime(2026, 7, 14, 1, 30, tzinfo=timezone.utc))
    assert window.contains(datetime(2026, 7, 14, 23, 30, tzinfo=timezone.utc))
    assert not window.contains(datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc))


def test_invalid_window_and_config_fail_closed():
    with pytest.raises(ValueError):
        MaintenanceWindow.parse("25:00-01:00")
    with pytest.raises(ValueError):
        GovernorConfig(max_concurrency=1, interactive_reserve=1)
