from __future__ import annotations

from blackholememory.retrieval_query_plan import SCHEMA_VERSION
from blackholememory.retrieval_query_plan import build_retrieval_query_plan


def test_query_plan_is_content_free_deterministic_and_reports_routes() -> None:
    hits = [
        {"id": "secret-memory-id", "context_origin": "LOCAL", "metadata": {}},
        {"id": "other-secret-id", "context_origin": "GLOBAL", "metadata": {"retrieval_route": "exact-identifier"}},
    ]
    first = build_retrieval_query_plan(
        requested_limit=5,
        offset=0,
        total_candidates=2,
        returned_hits=hits,
        duration_ms=1.23456,
        include_global=True,
        include_graph_expansion=False,
        typed_filter_requested=True,
        temporal_filter_requested=True,
    )
    second = build_retrieval_query_plan(
        requested_limit=5,
        offset=0,
        total_candidates=2,
        returned_hits=hits,
        duration_ms=1.23456,
        include_global=True,
        include_graph_expansion=False,
        typed_filter_requested=True,
        temporal_filter_requested=True,
    )

    assert first == second
    assert first["schema_version"] == SCHEMA_VERSION
    assert first["underfill_reason"] == "eligible_candidates_exhausted"
    assert first["stages"][2] == {
        "name": "candidate_routes",
        "returned_by_route": {"global:exact-identifier": 1, "local:vector": 1},
        "global_enabled": True,
    }
    assert "secret-memory-id" not in str(first)
    assert "other-secret-id" not in str(first)
    assert first["query_plan_builder"]["retrieval_policy_changed"] is False


def test_query_plan_reports_empty_candidates_and_bounds_untrusted_fields() -> None:
    plan = build_retrieval_query_plan(
        requested_limit=999,
        offset=-4,
        total_candidates=-1,
        returned_hits=[],
        duration_ms=999_999,
        include_global=False,
        include_graph_expansion=True,
        typed_filter_requested=False,
        temporal_filter_requested=False,
    )

    assert plan["requested_limit"] == 200
    assert plan["offset"] == 0
    assert plan["returned_candidates"] == 0
    assert plan["underfill_reason"] == "no_eligible_candidates"
    assert plan["duration_ms"] == 60_000.0
    assert plan["stages"][2]["returned_by_route"] == {}


def test_query_plan_accepts_only_content_free_allowlisted_contour_timings() -> None:
    plan = build_retrieval_query_plan(
        requested_limit=5,
        offset=0,
        total_candidates=1,
        returned_hits=[],
        duration_ms=1.0,
        include_global=True,
        include_graph_expansion=True,
        typed_filter_requested=False,
        temporal_filter_requested=False,
        contour_trace={
            "schema_version": "bhm.retrieval-contour-trace.v1",
            "total_duration_ms": 1.23456,
            "embedding": {"enabled": True, "duration_ms": 0.4, "query": "secret"},
            "contours": [
                {"name": "local_vector", "enabled": True, "status": "completed", "duration_ms": 0.5, "id": "secret-id"},
                {"name": "bad", "enabled": True, "status": "completed", "duration_ms": 99},
            ],
        },
    )

    timing = plan["stages"][3]
    assert timing == {
        "name": "contour_timings",
        "schema_version": "bhm.retrieval-contour-trace.v1",
        "total_duration_ms": 1.235,
        "embedding": {"enabled": True, "status": "completed", "duration_ms": 0.4},
        "contours": [{"name": "local_vector", "enabled": True, "status": "completed", "duration_ms": 0.5}],
    }
    assert "secret" not in str(plan)
    assert "secret-id" not in str(plan)
