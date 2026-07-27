from __future__ import annotations

from blackholememory.llm_final_gate import LLM_FINAL_GATE_JOB_COUNT
from blackholememory.llm_final_gate import run_final_gate


def test_final_gate_covers_100_jobs_and_all_safety_lifecycle_controls():
    report = run_final_gate()

    assert report["ok"] is True
    assert report["job_count"] == LLM_FINAL_GATE_JOB_COUNT == 100
    assert all(report["checks"].values())
    assert report["queue"]["counts"]["completed"] == 99
    assert report["queue"]["counts"]["cancelled"] == 1
    assert report["queue"]["pending"] == 0
    assert report["measured_value"]["value_over_manual_baseline"] is True
    assert report["execution_enabled"] is False
    assert report["writes_live_state"] is False
    assert report["model_calls"] == 0
