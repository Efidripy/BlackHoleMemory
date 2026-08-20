from __future__ import annotations

import pytest

from blackholememory.model_router import ModelRouterError
from blackholememory.model_router import route_model
from blackholememory.model_router import router_snapshot


def test_default_8k_profile_routes_local_coder_model():
    decision = route_model("code_review", required_capabilities=["coding", "reasoning"], context_tokens=8192)

    assert decision.status == "routed"
    assert decision.model_id == "qwen2.5-coder-7b-instruct"
    assert decision.profile_tokens == 8192
    assert "local_only_attestation" in decision.reason_codes
    assert decision.execution_enabled is False


def test_unmeasured_16k_and_32k_profiles_fail_closed():
    for tokens in (16384, 32768):
        decision = route_model("reasoning", required_capabilities=["reasoning"], context_tokens=tokens)
        assert decision.status == "rejected"
        assert "context_profile_not_measured" in decision.reason_codes


def test_explicit_measurement_unlocks_16k_profile():
    decision = route_model(
        "reasoning",
        required_capabilities=["reasoning"],
        context_tokens=16_000,
        measurements=[{"context_tokens": 16384, "ok": True, "latency_ms": 900, "tokens_per_second": 20}],
    )

    assert decision.status == "routed"
    assert decision.profile_tokens == 16384


def test_missing_vision_capability_is_not_cloud_fallback():
    decision = route_model("image_review", required_capabilities=["vision"], context_tokens=8192)

    assert decision.status == "rejected"
    assert decision.model_id is None
    assert "vision_capability_unconfirmed" in decision.reason_codes
    assert decision.local_only is True


def test_custom_inventory_selects_lowest_latency_matching_local_model():
    decision = route_model(
        "classify",
        required_capabilities=["classification", "json"],
        models=[
            {"model_id": "slow", "capabilities": ["classification", "json"], "context_window": 8192, "local_only": True, "available": True, "latency_ms": 100},
            {"model_id": "fast", "capabilities": ["classification", "json"], "context_window": 8192, "local_only": True, "available": True, "latency_ms": 20},
        ],
    )

    assert decision.model_id == "fast"


def test_custom_inventory_prefers_lighter_sufficient_tier_before_latency():
    decision = route_model(
        "classify",
        required_capabilities=["classification", "json"],
        models=[
            {
                "model_id": "deep-fast",
                "capabilities": ["classification", "json"],
                "context_window": 8192,
                "local_only": True,
                "available": True,
                "latency_ms": 5,
                "selection_tier": 3,
            },
            {
                "model_id": "light-slower",
                "capabilities": ["classification", "json"],
                "context_window": 8192,
                "local_only": True,
                "available": True,
                "latency_ms": 50,
                "selection_tier": 1,
            },
        ],
    )

    assert decision.model_id == "light-slower"
    assert decision.selection_tier == 1
    assert "minimum_sufficient_tier" in decision.reason_codes


def test_snapshot_and_invalid_capabilities_fail_closed():
    snapshot = router_snapshot()
    assert snapshot["cloud_fallback"] is False
    assert snapshot["selection_policy"]["strategy"] == "minimum_sufficient_local_tier"
    assert [item["profile_tokens"] for item in snapshot["context_profiles"]] == [8192, 16384, 32768]
    assert snapshot["context_profiles"][0]["status"] == "measured"
    with pytest.raises(ModelRouterError):
        route_model("bad", required_capabilities=["unknown"])
