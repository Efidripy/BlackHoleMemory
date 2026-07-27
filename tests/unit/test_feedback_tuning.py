from __future__ import annotations

from blackholememory.feedback_tuning import build_feedback_tuning
from blackholememory.feedback_tuning import summarize_quality_feedback


def test_tuning_waits_for_both_explicit_feedback_streams():
    result = build_feedback_tuning(
        usefulness={"explicit_memory_used": 4, "packed": 10},
        quality={"sample_size": 10, "average": 5},
    )
    assert result["status"] == "insufficient_feedback"
    assert result["recommendations"] == []
    assert result["mutation"] is False
    assert result["auto_apply"] is False


def test_high_feedback_produces_bounded_reviewable_budget_and_ranking_delta():
    result = build_feedback_tuning(
        usefulness={"explicit_memory_used": 8, "packed": 10},
        quality={"sample_size": 4, "average": 4.5},
        profile_budgets={"low-context": 350, "standard": 1200, "deep": 2400},
    )
    assert result["status"] == "reviewable_recommendations"
    assert result["requires_review"] is True
    budget = next(item for item in result["recommendations"] if item["kind"] == "budget")
    ranking = next(item for item in result["recommendations"] if item["kind"] == "ranking")
    assert budget["action"] == "increase_context_budget"
    assert budget["ratio"] == 0.1
    assert budget["budgets"]["deep"] == 2640
    assert abs(ranking["quality_weight_delta"]) <= 0.1
    assert len(result["preview_digest"]) == 64


def test_quality_summary_ignores_invalid_votes_and_memory_content():
    result = summarize_quality_feedback(
        [
            {"project": "blackholememory", "content": "secret", "metadata": {"quality_votes": [{"vote": 5}, {"vote": 0}, {"vote": "bad"}]}},
            {"project": "other", "metadata": {"quality_votes": [{"vote": 1}]}},
        ],
        project="blackholememory",
    )
    assert result == {"sample_size": 1, "average": 5.0, "minimum": 5, "maximum": 5}


def test_feedback_tuning_endpoint_is_review_only(monkeypatch):
    from blackholememory import app as bhm_app

    monkeypatch.setattr(
        bhm_app._RETRIEVAL_FUNNEL,
        "snapshot",
        lambda: {"groups": [{"project": "blackholememory", "packed": 10, "explicit_memory_used": 8}]},
    )
    monkeypatch.setattr(
        bhm_app,
        "_load_live_memories",
        lambda: [{"project": "blackholememory", "metadata": {"quality_votes": [{"vote": 5}] * 3}}],
    )

    result = bhm_app.bhm_feedback_tuning("blackholememory")
    assert result["mutation"] is False
    assert result["auto_apply"] is False
    assert result["source_signals"] == ["explicit_memory_used", "quality_vote"]
