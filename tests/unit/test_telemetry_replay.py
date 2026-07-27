from __future__ import annotations

from blackholememory.telemetry_replay import measure_overhead
from blackholememory.telemetry_replay import run_replay
from blackholememory.telemetry_replay import run_replay_gate


def test_replay_is_reproducible_and_privacy_safe():
    first = run_replay(usage_events=32, funnel_sessions=12)
    second = run_replay(usage_events=32, funnel_sessions=12)

    assert first["digest"] == second["digest"]
    assert first["usage"]["totals"]["count"] == 32
    assert first["funnel"]["totals"]["requests"] == 12
    assert first["usage"]["privacy"]["raw_payloads"] is False
    assert first["funnel"]["privacy"]["implicit_access_feedback"] is False
    assert "replay-memory-0-0" not in str(first)


def test_replay_gate_and_overhead_budget_pass():
    gate = run_replay_gate()
    assert gate["ok"] is True
    assert gate["reproducible"] is True
    assert measure_overhead(events=200, samples=3)["ok"] is True
