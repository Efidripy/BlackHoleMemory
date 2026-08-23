from __future__ import annotations

import hashlib

import pytest

from blackholememory.feedback_consolidation import FeedbackConsolidationError
from blackholememory.feedback_consolidation import SCHEMA_VERSION
from blackholememory.feedback_consolidation import build_feedback_consolidation_preview


def _utility_report(*rows: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "bhm.utility-feedback.v1",
        "report_digest": hashlib.sha256(b"utility-report").hexdigest(),
        "rows": list(rows),
    }


def _row(memory_id: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "project": "blackholememory",
        "memory_id": memory_id,
        "sample_count": 3,
        "score": -0.5,
        "uncertainty": "bounded",
        "event_counts": {"contradicted": 1, "corrected": 1},
        "lifecycle_action": "none",
    }
    row.update(overrides)
    return row


def test_preview_is_deterministic_content_free_and_never_applies_lifecycle_actions() -> None:
    report = _utility_report(_row("memory-b"), _row("memory-a", event_counts={"contradicted": 1}))

    first = build_feedback_consolidation_preview(report, project="blackholememory")
    second = build_feedback_consolidation_preview(report, project="blackholememory")

    assert first == second
    assert first["schema_version"] == SCHEMA_VERSION
    assert first["proposal_count"] == 5
    assert first["execution"]["auto_apply"] is False
    assert first["execution"]["automatic_lifecycle_action"] is False
    assert all(item["lifecycle_action"] == "none" for item in first["proposals"])
    assert all(item["recommended_action"] == "review_authoritative_evidence" for item in first["proposals"])


def test_insufficient_or_positive_feedback_does_not_create_a_review_item() -> None:
    preview = build_feedback_consolidation_preview(
        _utility_report(
            _row("few", sample_count=2),
            _row("positive", score=0.5, event_counts={}),
        ),
        project="blackholememory",
    )

    assert preview["proposals"] == []
    assert preview["proposal_count"] == 0


def test_preview_rejects_raw_content_field_and_does_not_promote_high_uncertainty() -> None:
    preview = build_feedback_consolidation_preview(
        _utility_report(_row("m", uncertainty="high")),
        project="blackholememory",
    )
    assert preview["proposals"] == []
    with pytest.raises(FeedbackConsolidationError, match="unsupported fields"):
        build_feedback_consolidation_preview(
            _utility_report(_row("m", content="private raw memory")),
            project="blackholememory",
        )


def test_preview_fails_closed_on_cross_project_or_lifecycle_report_row() -> None:
    with pytest.raises(FeedbackConsolidationError, match="project mismatch"):
        build_feedback_consolidation_preview(_utility_report(_row("m", project="other")), project="blackholememory")
    with pytest.raises(FeedbackConsolidationError, match="lifecycle_action"):
        build_feedback_consolidation_preview(
            _utility_report(_row("m", lifecycle_action="archive")),
            project="blackholememory",
        )
