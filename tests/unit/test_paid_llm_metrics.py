from __future__ import annotations

from blackholememory import paid_llm


def setup_function() -> None:
    with paid_llm._METRIC_LOCK:
        paid_llm._METRICS.clear()


def teardown_function() -> None:
    with paid_llm._METRIC_LOCK:
        paid_llm._METRICS.clear()


def test_snapshot_aggregates_calls_latency_and_error_class() -> None:
    paid_llm.record_call("doc2query", True, 10)
    try:
        raise ValueError("fixture failure")
    except ValueError:
        paid_llm.record_call("doc2query", False, 30)

    assert paid_llm.snapshot() == {
        "doc2query": {
            "calls": 2,
            "errors": 1,
            "last_error_class": "ValueError",
            "avg_ms": 20.0,
            "max_ms": 30.0,
        }
    }


def test_snapshot_caps_distinct_sites() -> None:
    for index in range(20):
        paid_llm.record_call(f"site-{index}", True, 1)

    assert len(paid_llm.snapshot()) == 16
