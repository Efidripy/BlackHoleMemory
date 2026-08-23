from __future__ import annotations

import pytest

from blackholememory.hybrid_retrieval_evaluation import RRF_K
from blackholememory.hybrid_retrieval_evaluation import RRF_WEIGHTS
from blackholememory.hybrid_retrieval_evaluation import build_exact_identifier_candidate_index
from blackholememory.hybrid_retrieval_evaluation import build_hybrid_retrieval_cases
from blackholememory.hybrid_retrieval_evaluation import build_sqlite_fts5_candidate_index
from blackholememory.hybrid_retrieval_evaluation import candidate_augmented_rank
from blackholememory.hybrid_retrieval_evaluation import evaluate_hybrid_retrieval
from blackholememory.hybrid_retrieval_evaluation import exact_identifier_rank
from blackholememory.hybrid_retrieval_evaluation import fixed_rrf_rank
from blackholememory.hybrid_retrieval_evaluation import promotion_recommendation
from blackholememory.hybrid_retrieval_evaluation import sqlite_fts5_bm25_rank


def _stable_current(case):
    return list(case.semantic_candidate_ids)


def test_fixture_is_bounded_and_fts5_rejects_cross_project_archived_and_log_hits() -> None:
    cases = build_hybrid_retrieval_cases(100)
    assert len(cases) == 100
    identifier_case = next(case for case in cases if case.scenario == "identifier_recovery")
    connection = build_sqlite_fts5_candidate_index(cases)
    try:
        ranked = sqlite_fts5_bm25_rank(connection, identifier_case)
    finally:
        connection.close()

    assert identifier_case.relevant_ids.issubset(ranked)
    assert not any("cross-project" in source_id or "archived" in source_id or source_id.endswith("-log") for source_id in ranked)


def test_exact_identifier_route_is_project_scoped_and_excludes_inactive_rows() -> None:
    cases = build_hybrid_retrieval_cases(100)
    identifier_case = next(case for case in cases if case.scenario == "identifier_recovery")
    connection = build_exact_identifier_candidate_index(cases)
    try:
        ranked = exact_identifier_rank(connection, identifier_case)
        no_identifier_case = next(case for case in cases if case.scenario == "paraphrase_semantic")
        assert exact_identifier_rank(connection, no_identifier_case) == []
    finally:
        connection.close()

    assert identifier_case.relevant_ids.issubset(ranked)
    assert not any("cross-project" in source_id or "archived" in source_id or source_id.endswith("-log") for source_id in ranked)


def test_evaluation_is_deterministic_without_external_backends() -> None:
    cases = build_hybrid_retrieval_cases(100)
    first = evaluate_hybrid_retrieval(cases=cases, repeats=3, current_ranker=_stable_current)
    second = evaluate_hybrid_retrieval(cases=cases, repeats=3, current_ranker=_stable_current)

    assert first["fixture_digest"] == second["fixture_digest"]
    for mode in ("current_bhm", "current_plus_fts5_candidate", "fixed_rrf", "current_plus_exact_identifier", "exact_identifier_fixed_rrf"):
        for metric in ("recall_at_5", "mrr", "project_leakage_count", "cases"):
            assert first["modes"][mode][metric] == second["modes"][mode][metric]
    assert first["execution"] == {
        "sqlite_mode": "in-memory-fts5-fixture-only",
        "authoritative_sqlite_opened": False,
        "sqlite_written": False,
        "qdrant_called": False,
        "mem0_called": False,
        "model_called": False,
        "network_called": False,
        "production_retrieval_changed": False,
    }


def test_candidate_augmentation_and_fixed_rrf_are_deterministic_and_configured() -> None:
    current = ["semantic-a", "semantic-b"]
    lexical = ["target", "semantic-a"]

    assert candidate_augmented_rank(current, lexical) == ["semantic-a", "semantic-b", "target"]
    assert fixed_rrf_rank(current, lexical) == ["semantic-a", "target", "semantic-b"]
    assert RRF_K == 60
    assert RRF_WEIGHTS == {"current_bhm": 1.0, "sqlite_fts5_bm25": 1.0}


def test_promotion_requires_every_threshold() -> None:
    modes = {
        "current_bhm": {"recall_at_5": 0.8, "p95_latency_ms": 1.0, "project_leakage_count": 0},
        "current_plus_fts5_candidate": {"recall_at_5": 0.9, "p95_latency_ms": 1.1, "project_leakage_count": 0},
        "fixed_rrf": {"recall_at_5": 0.9, "p95_latency_ms": 1.2, "project_leakage_count": 0},
    }
    assert promotion_recommendation(modes)["eligible_for_feature_flag_proposal"] is True
    modes["fixed_rrf"]["project_leakage_count"] = 1
    assert promotion_recommendation(modes)["decision"] == "defer"


def test_rejects_out_of_range_fixture_sizes() -> None:
    with pytest.raises(ValueError, match="between 100 and 200"):
        build_hybrid_retrieval_cases(99)
    with pytest.raises(ValueError, match="between 100 and 200"):
        build_hybrid_retrieval_cases(201)
