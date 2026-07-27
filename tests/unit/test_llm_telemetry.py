from __future__ import annotations

from types import SimpleNamespace

from blackholememory.llm_telemetry import LLMTelemetry


def test_telemetry_aggregates_gateway_metrics_without_raw_content():
    telemetry = LLMTelemetry(max_groups=4, max_samples=4)
    telemetry.record(
        job_type="memory-summary",
        workload="background",
        project="blackholememory",
        status="completed",
        queue_wait_ms=12,
        latency_ms=200,
        prompt_tokens=100,
        completion_tokens=40,
        schema_pass=True,
        validator_pass=True,
        outcome="accepted",
        retry_count=1,
        fallback=False,
        usefulness="positive",
        gpu_temperature_c=60,
        gpu_vram_used_ratio=0.7,
        gpu_utilization_percent=80,
    )
    snapshot = telemetry.snapshot()
    group = snapshot["groups"][0]
    assert snapshot["privacy"]["raw_prompts"] is False
    assert group["accepted"] == 1
    assert group["schema_pass_rate"] == 1.0
    assert group["latency_ms"]["p50"] == 200.0
    assert group["tokens_per_second"]["p50"] == 200.0
    assert "secret output" not in str(snapshot)


def test_telemetry_uses_overflow_group_and_explicit_evaluation_only():
    telemetry = LLMTelemetry(max_groups=2, max_samples=2)
    telemetry.record(job_type="one", workload="foreground", project="p1", outcome="unknown")
    telemetry.record(job_type="two", workload="foreground", project="p2", outcome="rejected", usefulness="negative")
    snapshot = telemetry.snapshot()
    assert len(snapshot["groups"]) == 2
    assert snapshot["totals"]["rejected"] == 1
    assert snapshot["totals"]["usefulness"]["negative"] == 1


def test_record_gateway_result_extracts_usage_and_schema_without_content():
    telemetry = LLMTelemetry()
    result = SimpleNamespace(
        ok=True,
        latency_ms=150,
        usage={"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
        validation={"checked": True, "ok": True},
        failure=None,
        content="secret output must not be stored",
    )
    telemetry.record_gateway_result(
        result,
        job_type="probe",
        workload="interactive",
        project="demo",
        outcome="accepted",
    )
    snapshot = telemetry.snapshot()
    assert snapshot["totals"]["tokens"]["total"] == 15
    assert snapshot["totals"]["schema_pass"] == 1
    assert "secret output" not in str(snapshot)


def test_telemetry_reset_clears_process_window():
    telemetry = LLMTelemetry()
    telemetry.record(job_type="probe", workload="foreground", project="demo")
    telemetry.reset()
    assert telemetry.snapshot()["totals"]["jobs"] == 0
