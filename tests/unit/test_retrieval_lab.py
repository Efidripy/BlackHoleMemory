from __future__ import annotations

from datetime import datetime, timezone

import pytest

from blackholememory.retrieval_lab import RetrievalLabError
from blackholememory.retrieval_lab import build_retrieval_lab_preview
from blackholememory.retrieval_lab import verify_retrieval_lab_digest


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _candidate(candidate_id: str, content: str, *, project: str = "demo", score: float = 0.8, semantic_type: str = "feature") -> dict:
    return {
        "id": candidate_id,
        "content": content,
        "score": score,
        "metadata": {
            "source_id": candidate_id,
            "project": project,
            "semantic_type": semantic_type,
            "lifecycle": "validated",
        },
    }


def test_preview_builds_experiments_and_is_digest_verifiable():
    preview = build_retrieval_lab_preview(
        "retrieval contract",
        project="demo",
        candidates=[_candidate("m1", "retrieval contract implementation evidence")],
        now=NOW,
    )

    assert preview["schema_version"] == "bhm.llm.retrieval-lab.v1"
    assert preview["query_rewrites"]
    assert len(preview["multi_queries"]) >= 2
    assert preview["hyde_candidates"]
    assert preview["reranked"][0]["candidate_id"] == "m1"
    assert preview["execution"]["writes_performed"] is False
    assert verify_retrieval_lab_digest(preview) is True


def test_filter_and_leakage_gate_rejects_other_project_archived_and_logs():
    candidates = [
        _candidate("ok", "retrieval contract", score=0.9),
        _candidate("other", "retrieval contract", project="other", score=0.99),
        {**_candidate("archived", "retrieval contract"), "metadata": {"source_id": "archived", "project": "demo", "lifecycle": "archived"}},
        _candidate("log", "retrieval contract", semantic_type="log"),
    ]
    preview = build_retrieval_lab_preview("retrieval contract", project="demo", candidates=candidates, now=NOW)

    assert preview["filter_gate"]["passed"] is False
    assert preview["filter_gate"]["leakage_count"] == 3
    assert preview["gates"]["leakage"]["passed"] is False
    assert any(case["code"] == "project_or_lifecycle_leakage" for case in preview["failure_cases"])


def test_feature_flags_disable_experiments_fail_closed_for_unknown_flags():
    preview = build_retrieval_lab_preview(
        "query",
        project="demo",
        candidates=[_candidate("m1", "query")],
        feature_flags={"query_rewrite": False, "multi_query": False, "hyde": False, "rerank": False, "hard_negatives": False, "synthetic_benchmark": False, "failure_cases": False},
        now=NOW,
    )
    assert preview["query_rewrites"] == []
    assert preview["multi_queries"] == ["query"]
    assert preview["hyde_candidates"] == []
    assert preview["synthetic_benchmark"] == []
    assert preview["failure_cases"] == []
    with pytest.raises(RetrievalLabError):
        build_retrieval_lab_preview("query", feature_flags={"unknown": True})


def test_hard_negatives_and_synthetic_benchmark_are_bounded_review_artifacts():
    preview = build_retrieval_lab_preview(
        "retrieval contract",
        project="demo",
        candidates=[_candidate("good", "retrieval contract evidence", score=0.9), _candidate("weak", "unrelated", score=0.2)],
        benchmark_cases=4,
        now=NOW,
    )
    assert preview["hard_negatives"]
    assert len(preview["synthetic_benchmark"]) == 4
    assert all(case["label_required"] and case["evaluation_only"] for case in preview["synthetic_benchmark"])
    assert preview["gates"]["benchmark_requires_labels"] is True


def test_latency_gate_generates_failure_case_without_hiding_breach():
    preview = build_retrieval_lab_preview(
        "query",
        project="demo",
        candidates=[_candidate("m1", "query")],
        latency_budget_ms=10,
        observed_latency_ms=25,
        now=NOW,
    )
    assert preview["latency_gate"]["status"] == "breached"
    assert preview["latency_gate"]["passed"] is False
    assert any(case["code"] == "latency_budget_breached" for case in preview["failure_cases"])


def test_bounds_fail_closed():
    with pytest.raises(RetrievalLabError):
        build_retrieval_lab_preview("query", limit=0)
    with pytest.raises(RetrievalLabError):
        build_retrieval_lab_preview("query", benchmark_cases=33)
