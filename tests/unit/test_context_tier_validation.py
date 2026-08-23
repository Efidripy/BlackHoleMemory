from __future__ import annotations

from blackholememory.context_tier_validation import SCHEMA_VERSION
from blackholememory.context_tier_validation import build_context_tier_validation_report


def test_context_tier_validation_is_green_and_content_free() -> None:
    report = build_context_tier_validation_report(iterations=4, p95_budget_ms=1_000.0)

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["ok"] is True
    assert all(report["checks"].values())
    assert report["context"]["included_tiers"] == ["working", "session", "project", "archival"]
    assert report["execution"] == {
        "network": False,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "promotion": "none",
    }
    assert "synthetic-source-a" not in str(report)


def test_context_tier_validation_rejects_unbounded_inputs() -> None:
    try:
        build_context_tier_validation_report(iterations=1)
    except ValueError as exc:
        assert str(exc) == "iterations must be at least 2"
    else:
        raise AssertionError("iterations=1 must be rejected")
