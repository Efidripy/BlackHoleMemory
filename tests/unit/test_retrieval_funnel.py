from __future__ import annotations

from blackholememory.retrieval_funnel import RetrievalFunnel
from blackholememory.retrieval_funnel import normalize_dimension


def test_retrieval_funnel_tracks_stages_and_explicit_use_only():
    funnel = RetrievalFunnel(explicit_use_ttl_seconds=10)
    funnel.record_context(
        project="blackholememory",
        profile="standard",
        surface="rest",
        requested_count=4,
        eligible_count=3,
        packed_count=2,
        cited_count=2,
        item_ids=["memory-a", "memory-b"],
        now=0,
    )

    pending = funnel.snapshot(now=1)
    assert pending["totals"]["packed"] == 2
    assert pending["totals"]["pending_requests"] == 1
    assert pending["groups"][0]["project"] == "blackholememory"
    assert pending["groups"][0]["surface"] == "rest"

    assert funnel.record_memory_used(project="blackholememory", item_ids=["memory-a"], now=2) == 1
    partial = funnel.snapshot(now=2)
    assert partial["totals"]["explicit_memory_used"] == 1
    assert partial["totals"]["pending_items"] == 1

    expired = funnel.snapshot(now=11)
    assert expired["totals"]["pending_requests"] == 0
    assert expired["totals"]["unused_requests"] == 0
    assert expired["totals"]["unused_items"] == 1


def test_retrieval_funnel_marks_empty_and_fully_used_requests_resolved():
    funnel = RetrievalFunnel(explicit_use_ttl_seconds=10)
    funnel.record_context(
        project="p",
        profile="low-context",
        surface="mcp",
        requested_count=0,
        eligible_count=0,
        packed_count=0,
        cited_count=0,
        now=0,
    )
    funnel.record_context(
        project="p",
        profile="low-context",
        surface="mcp",
        requested_count=2,
        eligible_count=2,
        packed_count=2,
        cited_count=2,
        item_ids=["a", "b"],
        now=1,
    )
    assert funnel.record_memory_used(project="p", item_ids=["a", "b"], now=2) == 2

    snapshot = funnel.snapshot(now=2)
    group = snapshot["groups"][0]
    assert group["empty_requests"] == 1
    assert group["empty_rate"] == 0.5
    assert group["explicit_memory_used"] == 2
    assert group["pending_requests"] == 0
    assert group["unused_requests"] == 0


def test_retrieval_funnel_is_bounded_and_does_not_return_ids():
    funnel = RetrievalFunnel(max_groups=2, max_pending_sessions=2)
    for index in range(6):
        funnel.record_context(
            project=f"project-{index}",
            profile="standard",
            surface="rest",
            requested_count=1,
            eligible_count=1,
            packed_count=1,
            cited_count=1,
            item_ids=[f"secret-memory-id-{index}"],
            now=float(index),
        )

    snapshot = funnel.snapshot(now=100)
    assert len(snapshot["groups"]) <= 2
    assert snapshot["privacy"]["implicit_access_feedback"] is False
    assert "secret-memory-id-0" not in str(snapshot)


def test_normalize_dimension_is_bounded():
    assert normalize_dimension("project/name?secret") == "project_name_secret"
    assert len(normalize_dimension("x" * 200)) == 64
