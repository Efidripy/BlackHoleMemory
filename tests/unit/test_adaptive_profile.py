from __future__ import annotations

import pytest

from blackholememory.adaptive_profile import recommend_context_profile
from blackholememory.adaptive_profile import summarize_explicit_usefulness


def test_recommendation_is_explainable_and_advisory_by_default():
    result = recommend_context_profile("simple status", historical_usefulness=None)

    assert result["recommended_profile"] == "low-context"
    assert result["applied_profile"] == "standard"
    assert result["mode"] == "advisory"
    assert result["manual_override_required"] is True
    assert result["auto_apply"] is False
    assert result["reasons"]


def test_complex_query_and_explicit_usefulness_recommend_deep():
    result = recommend_context_profile(
        "Explain architecture dependency regression migration tradeoffs and compare root-cause evidence",
        historical_usefulness={"requests": 4, "packed": 8, "explicit_memory_used": 8, "unused_requests": 0},
        filter_count=3,
    )

    assert result["recommended_profile"] == "deep"
    assert result["complexity"]["filter_count"] == 3
    assert "historically_useful_context" in result["reasons"]


def test_manual_override_is_respected_without_auto_mutation():
    result = recommend_context_profile(
        "simple status",
        requested_profile="deep-context",
        default_profile="low-context",
    )

    assert result["recommended_profile"] == "low-context"
    assert result["applied_profile"] == "deep"
    assert result["mode"] == "manual_override"
    assert "manual_override_respected" in result["reasons"]


def test_usefulness_summary_uses_only_explicit_feedback_and_bounds_values():
    result = summarize_explicit_usefulness(
        {
            "groups": [
                {"project": "blackholememory", "requests": 4, "packed": 5, "explicit_memory_used": 2, "unused_requests": 1},
                {"project": "other", "requests": 100, "packed": 100, "explicit_memory_used": 100, "unused_requests": 0},
            ]
        },
        project="blackholememory",
    )

    assert result == {
        "sample_size": 4,
        "packed": 5,
        "explicit_memory_used": 2,
        "unused_requests": 1,
        "explicit_use_rate": 0.4,
        "unused_request_rate": 0.25,
    }


def test_invalid_manual_profile_fails_closed():
    with pytest.raises(ValueError, match="unknown context profile"):
        recommend_context_profile("query", requested_profile="experimental")
