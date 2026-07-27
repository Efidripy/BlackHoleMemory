from __future__ import annotations

from blackholememory.context_confidence import assess_context_confidence


def test_empty_context_is_insufficient_and_confirmation_gated():
    result = assess_context_confidence(
        hits=[],
        included_count=0,
        citations=[],
        total_candidates=0,
        token_budget=1200,
    )

    assert result["insufficient_context"] is True
    assert result["confidence"] == 0.0
    assert result["follow_up"]["action"] == "request_additional_context"
    assert result["follow_up"]["requires_confirmation"] is True
    assert result["follow_up"]["auto_retrieval"] is False


def test_evidenced_high_score_context_is_sufficient():
    result = assess_context_confidence(
        hits=[{"id": "secret-id", "score": 0.96}],
        included_count=1,
        citations=[{"provenance": {"evidence_complete": True}}],
        total_candidates=1,
        token_budget=1200,
    )

    assert result["insufficient_context"] is False
    assert result["level"] == "high"
    assert result["follow_up"]["action"] == "none"


def test_low_signal_follow_up_is_bounded_and_does_not_echo_inputs():
    result = assess_context_confidence(
        hits=[{"id": "private-memory-123", "score": 0.2}],
        included_count=1,
        citations=[{"provenance": {"evidence_complete": False}}],
        total_candidates=4,
        token_budget=64,
        omitted_count=3,
    )

    serialized = str(result["follow_up"])
    assert result["insufficient_context"] is True
    assert "private-memory-123" not in serialized
    assert len(result["reason_codes"]) <= 6
