from __future__ import annotations

from datetime import datetime, timedelta, timezone

from blackholememory.semantic_observation import build_semantic_observation
from blackholememory.semantic_observation import build_semantic_freshness_receipt


def _snapshot(completed_at: str | None) -> dict[str, str]:
    return {
        "completed_at": completed_at or "",
        "snapshot_digest": "a" * 64,
    }


def test_not_requested_does_not_claim_semantic_quality() -> None:
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    receipt = build_semantic_observation(
        _snapshot("2026-07-23T11:59:00+00:00"),
        requested=False,
        request_status="not_requested",
        active=False,
        now=now,
    )
    assert receipt["status"] == "not_requested"
    assert receipt["freshness"]["status"] == "fresh"
    assert receipt["execution"]["writes_qdrant"] is False
    assert len(receipt["evidence_digest"]) == 64


def test_requested_fresh_fusion_is_a_gap_without_provider_slo() -> None:
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    receipt = build_semantic_observation(
        _snapshot("2026-07-23T11:59:00+00:00"),
        requested=True,
        request_status="enabled",
        active=True,
        now=now,
    )
    assert receipt["status"] == "gap"
    assert receipt["freshness"]["status"] == "fresh"
    assert "provider_slo_observation_missing" in receipt["gaps"]
    assert "semantic_relevance_not_evaluated" in receipt["gaps"]


def test_stale_snapshot_fails_closed() -> None:
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    stale = (now - timedelta(days=2)).isoformat()
    receipt = build_semantic_observation(
        _snapshot(stale),
        requested=True,
        request_status="enabled",
        active=False,
        now=now,
    )
    assert receipt["status"] == "fail"
    assert receipt["freshness"]["status"] == "stale"
    assert "snapshot_age_outside_budget" in receipt["failures"]


def test_invalid_timestamp_remains_an_explicit_gap() -> None:
    receipt = build_semantic_observation(
        _snapshot("not-a-timestamp"),
        requested=True,
        request_status="enabled",
        active=False,
        now=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    assert receipt["status"] == "gap"
    assert receipt["freshness"]["status"] == "unknown"
    assert "snapshot_completed_at_missing" in receipt["gaps"]


def test_freshness_receipt_binds_feature_provider_runtime_graph_and_latency() -> None:
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    receipt = build_semantic_freshness_receipt(
        _snapshot("2026-07-23T11:59:00+00:00"),
        requested=True,
        feature_enabled=True,
        request_status="enabled",
        active=True,
        provider_ready=True,
        runtime_slo_status="healthy",
        graph_snapshot_id="graph-1",
        graph_digest="b" * 64,
        observed_latency_ms=120.0,
        now=now,
    )
    assert receipt["schema_version"] == "bhm.semantic-freshness-receipt.v1"
    assert receipt["status"] == "fresh"
    assert receipt["freshness"]["status"] == "fresh"
    assert receipt["provider"]["status"] == "ready"
    assert receipt["runtime"]["graph_bound"] is True
    assert receipt["latency_slo"]["status"] == "within_budget"
    assert receipt["gaps"] == []
    assert receipt["execution"]["writes_qdrant"] is False
    assert len(receipt["evidence_digest"]) == 64


def test_freshness_receipt_stale_and_latency_budget_fail_closed() -> None:
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    stale = build_semantic_freshness_receipt(
        _snapshot("2026-07-21T12:00:00+00:00"),
        requested=True,
        feature_enabled=True,
        request_status="enabled",
        active=True,
        provider_ready=True,
        runtime_slo_status="healthy",
        observed_latency_ms=2_000.0,
        latency_budget_ms=250.0,
        now=now,
        max_snapshot_age_seconds=60.0,
    )
    assert stale["status"] == "stale"
    assert stale["freshness"]["status"] == "stale"
    assert "snapshot_age_outside_budget" in stale["failures"]

    fast_stale = build_semantic_freshness_receipt(
        _snapshot("2026-07-23T11:59:00+00:00"),
        requested=True,
        feature_enabled=True,
        request_status="enabled",
        active=True,
        provider_ready=True,
        runtime_slo_status="healthy",
        observed_latency_ms=2_000.0,
        latency_budget_ms=250.0,
        now=now,
    )
    assert fast_stale["status"] == "fail"
    assert "semantic_latency_budget_exceeded" in fast_stale["failures"]


def test_freshness_receipt_keeps_disabled_flag_explicit() -> None:
    receipt = build_semantic_freshness_receipt(
        _snapshot("2026-07-23T11:59:00+00:00"),
        requested=True,
        feature_enabled=False,
        request_status="feature_disabled",
        active=False,
    )
    assert receipt["status"] == "disabled"
    assert receipt["feature_flag"]["name"] == "BHM_CODE_SEMANTIC_FUSION"
    assert receipt["feature_flag"]["enabled"] is False
