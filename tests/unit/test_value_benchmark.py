from __future__ import annotations

from collections import Counter

import pytest

from blackholememory.value_benchmark import build_value_benchmark_cases
from blackholememory.value_benchmark import run_value_benchmark


def test_value_benchmark_has_realistic_hard_negatives_and_bhm_wins_proxy():
    report = run_value_benchmark(repeats=2, case_count=100)

    assert report["evidence_class"] == "deterministic-local-replay"
    assert report["execution"]["model_called"] is False
    assert report["execution"]["live_runtime_used"] is False
    assert report["aggregates"]["bhm-full"]["task_success_rate"] >= 0.8
    assert report["aggregates"]["bhm-full"]["leakage_count"] == 0
    assert report["aggregates"]["bhm-full"]["task_success_rate"] > report["aggregates"]["file-only"]["task_success_rate"]
    assert report["aggregates"]["bhm-full"]["task_success_rate"] > report["aggregates"]["naive-vector"]["task_success_rate"]
    assert report["aggregates"]["bhm-full"]["task_success_rate"] > report["aggregates"]["bhm-no-graph"]["task_success_rate"]
    assert report["aggregates"]["bhm-full"]["task_success_rate"] > report["aggregates"]["bhm-no-filters"]["task_success_rate"]


def test_value_benchmark_fixture_digest_is_stable():
    first = run_value_benchmark(repeats=1, case_count=100)
    second = run_value_benchmark(repeats=1, case_count=100)

    assert first["fixture_digest"] == second["fixture_digest"]
    assert first["report_digest"] == second["report_digest"]


def test_value_benchmark_rejects_unbounded_inputs():
    with pytest.raises(ValueError, match="repeats must be between"):
        run_value_benchmark(repeats=0)
    with pytest.raises(ValueError, match="count must be between"):
        run_value_benchmark(repeats=1, case_count=99)
    with pytest.raises(ValueError, match="count must be between"):
        build_value_benchmark_cases(1001)


def test_large_fixture_is_unique_and_covers_variants():
    cases = build_value_benchmark_cases(1000)

    assert len({case.case_id for case in cases}) == 1000
    assert len({case.target_id for case in cases}) == 1000
    assert set(Counter(case.variant for case in cases)) == {
        "direct",
        "paraphrase",
        "graph",
        "scope",
        "stale",
        "conflict",
        "handoff",
        "tie",
    }
    assert all(len(case.hits) == 6 for case in cases)
