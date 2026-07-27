from __future__ import annotations

from blackholememory.mcp_slo import classify_registration_inventory
from blackholememory.mcp_slo import evaluate_slo
from blackholememory.mcp_slo import percentile


def _green_kwargs() -> dict:
    return {
        "cold_initialize_catalog_ms": [1000.0, 2000.0, 3000.0],
        "recovery_attach_ms": [4000.0],
        "catalog_missing_states": 0,
        "active_duplicate_registrations": 0,
        "unauthorized_fallbacks_or_writes": 0,
        "samples": 3,
        "successful_samples": 3,
        "retry_count": 0,
        "registration": {
            "active_target_ok": True,
            "retained_residue_explicit": True,
            "retained_cache_residue": 2,
        },
        "adapter_check_ok": True,
        "runtime_healthy_before": True,
        "runtime_healthy_after": True,
        "telemetry_fallback_uses": 0,
        "writes_live_state": False,
        "slo_healthy_before": True,
        "slo_healthy_after": True,
        "outbox_failed_before": 0,
        "outbox_failed_after": 0,
        "outbox_pending_after": 0,
        "outbox_dead_letter_after": 0,
    }


def test_slo_percentile_is_bounded_and_deterministic():
    assert percentile([1, 2, 3, 4]) == 4.0
    assert percentile([]) is None


def test_registration_classification_does_not_hide_active_conflicts():
    active = classify_registration_inventory(
        {
            "registration_count": 7,
            "issues": [
                {"code": "alias_registration"},
                {"code": "duplicate_fingerprint"},
            ],
        }
    )
    assert active["active_target_ok"] is False
    assert active["retained_cache_residue"] == 0
    assert active["unknown_issue_count"] == 2
    active = classify_registration_inventory({"registration_count": 1, "issues": [{"code": "canonical_fingerprint_drift"}]})
    assert active["active_target_ok"] is False
    assert active["active_duplicate_registrations"] == 1


def test_slo_gate_requires_zero_error_budget_burn():
    assert evaluate_slo(**_green_kwargs())["ok"] is True
    failing = _green_kwargs()
    failing["catalog_missing_states"] = 1
    assert evaluate_slo(**failing)["ok"] is False
