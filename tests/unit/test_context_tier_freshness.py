from __future__ import annotations

import hashlib

import pytest

from blackholememory.context_tier_freshness import ProjectionState
from blackholememory.context_tier_freshness import TierFreshnessRecord
from blackholememory.context_tier_freshness import build_context_tier_freshness_report


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _record(memory_id: str, tier: str, *, source: str = "current", compiled: str = "current", present: bool = True, projection: ProjectionState = ProjectionState.READY) -> TierFreshnessRecord:
    return TierFreshnessRecord(
        memory_id=memory_id,
        tier=tier,
        compiled_source_digest=_digest(compiled),
        current_source_digest=_digest(source) if present else None,
        source_present=present,
        projection_state=projection,
    )


def test_freshness_report_is_deterministic_content_free_and_does_not_change_context_selection() -> None:
    fresh = _record("memory-fresh", "working")
    stale = _record("memory-stale", "project", source="new", compiled="old", projection=ProjectionState.PENDING)
    missing = _record("memory-missing", "archival", present=False, projection=ProjectionState.MISSING)

    one = build_context_tier_freshness_report((fresh, stale, missing), as_of="2026-08-23T20:00:00Z")
    two = build_context_tier_freshness_report((missing, fresh, stale), as_of="2026-08-23T20:00:00Z")

    assert one == two
    assert one["record_count"] == 3
    assert one["review_proposal_count"] == 2
    assert {item["source_state"] for item in one["records"]} == {"fresh", "stale", "missing"}
    assert all("memory-" not in str(item) for item in one["records"])
    assert one["execution"] == {
        "read_only": True,
        "network": False,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "context_selection_changed": False,
        "promotion": "none",
    }


def test_pending_projection_is_visible_but_only_failed_or_missing_projection_proposes_review() -> None:
    pending = _record("memory-pending", "session", projection=ProjectionState.PENDING)
    failed = _record("memory-failed", "project", projection=ProjectionState.FAILED)
    report = build_context_tier_freshness_report((pending, failed), as_of="2026-08-23T20:00:00Z")

    by_state = {item["projection_state"]: item for item in report["records"]}
    assert by_state["pending"]["context_eligibility"] == "sqlite_authoritative_only"
    assert by_state["failed"]["context_eligibility"] == "sqlite_authoritative_only"
    assert report["review_proposals"] == [{
        "memory_ref_digest": by_state["failed"]["memory_ref_digest"],
        "tier": "project",
        "reason": "projection_failed",
        "action": "operator_review_required",
    }]


def test_freshness_contract_rejects_conflicting_duplicate_evidence_and_invalid_bounds() -> None:
    baseline = _record("memory-a", "working")
    conflicting = _record("memory-a", "working", source="new", compiled="old")
    with pytest.raises(ValueError, match="conflicting"):
        build_context_tier_freshness_report((baseline, conflicting), as_of="2026-08-23T20:00:00Z")
    with pytest.raises(ValueError, match="max_proposals"):
        build_context_tier_freshness_report((baseline,), as_of="2026-08-23T20:00:00Z", max_proposals=0)
