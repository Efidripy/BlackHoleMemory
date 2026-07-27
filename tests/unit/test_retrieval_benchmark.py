from __future__ import annotations

import pytest

from blackholememory.retrieval_benchmark import build_default_benchmark_cases
from blackholememory.retrieval_benchmark import evaluate_benchmark
from blackholememory.retrieval_benchmark import filter_benchmark_hits


def test_default_benchmark_covers_required_case_range_and_filters_leakage():
    cases = build_default_benchmark_cases()

    assert len(cases) == 120
    filtered = filter_benchmark_hits(cases[0].hits, project=cases[0].project)
    assert {hit["metadata"]["source_id"] for hit in filtered} == {
        "relevant-0",
        "distractor-0",
        "graph-0",
    }


def test_benchmark_metrics_are_deterministic_and_bounded():
    cases = build_default_benchmark_cases(100)

    def score_ranker(_query: str, hits: list[dict]):
        return sorted(hits, key=lambda hit: float(hit.get("score") or 0.0), reverse=True)

    report = evaluate_benchmark(cases, ranker=score_ranker, include_case_reports=True)

    assert report["ok"] is True
    assert report["cases"] == 100
    assert report["top1_accuracy"] == 1.0
    assert report["ndcg_at_5"] == 1.0
    assert report["filter_correctness"] == 1.0
    assert report["context_budget_pass_rate"] == 1.0
    assert report["leakage_count"] == 0
    assert len(report["case_reports"]) == 100


def test_benchmark_rejects_case_counts_outside_gate():
    with pytest.raises(ValueError, match="between 100 and 200"):
        build_default_benchmark_cases(99)
    with pytest.raises(ValueError, match="between 100 and 200"):
        build_default_benchmark_cases(201)
