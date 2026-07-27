from blackholememory.mcp_final_gate import EXPECTED_CLIENTS
from blackholememory.mcp_final_gate import ROUTINE_COLD_ATTACHES
from blackholememory.mcp_final_gate import ROUTINE_RECOVERY_CYCLES
from blackholememory.mcp_final_gate import REQUIRED_COLD_ATTACHES
from blackholememory.mcp_final_gate import REQUIRED_RECOVERY_CYCLES
from blackholememory.mcp_final_gate import evaluate_final_gate
from blackholememory.mcp_final_gate import latency_summary
from blackholememory.mcp_final_gate import percentile
from blackholememory.mcp_soak import contains_forbidden


def _green_kwargs() -> dict:
    return {
        "cold_requested": REQUIRED_COLD_ATTACHES,
        "cold_successful": REQUIRED_COLD_ATTACHES,
        "cold_catalog_missing": 0,
        "cold_initialize_catalog_p95_ms": 3000.0,
        "recovery_requested": REQUIRED_RECOVERY_CYCLES,
        "recovery_successful": REQUIRED_RECOVERY_CYCLES,
        "recovery_restart_failures": 0,
        "recovery_catalog_missing": 0,
        "recovery_attach_after_ready_p95_ms": 5000.0,
        "matrix_ok": True,
        "matrix_client_count": len(EXPECTED_CLIENTS),
        "current_client_proof_ok": True,
        "config_canary_ok": True,
        "config_rollback_ok": True,
        "config_live_check_ok": True,
        "config_targets_unchanged": True,
        "active_duplicate_registrations": 0,
        "retained_residue_explicit": True,
        "unauthorized_fallbacks_or_writes": 0,
        "telemetry_fallback_uses": 0,
        "runtime_healthy_before": True,
        "runtime_healthy_after": True,
        "slo_healthy_before": True,
        "slo_healthy_after": True,
        "outbox_pending_after": 0,
        "outbox_failed_after": 0,
        "outbox_dead_letter_after": 0,
        "active_leases_after": 0,
        "owned_processes_after": 0,
        "ownership_records_clean": True,
        "writes_live_state": False,
    }


def test_final_gate_green_at_required_scale():
    result = evaluate_final_gate(**_green_kwargs())
    assert result["ok"] is True
    assert all(result["checks"].values())


def test_final_gate_rejects_short_or_broken_recovery_run():
    failing = _green_kwargs()
    failing["cold_requested"] = REQUIRED_COLD_ATTACHES - 1
    failing["recovery_restart_failures"] = 1
    assert evaluate_final_gate(**failing)["ok"] is False


def test_routine_profile_is_green_at_25_cold_and_10_recovery():
    routine = _green_kwargs()
    routine.update(
        {
            "cold_requested": ROUTINE_COLD_ATTACHES,
            "cold_successful": ROUTINE_COLD_ATTACHES,
            "recovery_requested": ROUTINE_RECOVERY_CYCLES,
            "recovery_successful": ROUTINE_RECOVERY_CYCLES,
            "required_cold_attaches": ROUTINE_COLD_ATTACHES,
            "required_recovery_cycles": ROUTINE_RECOVERY_CYCLES,
        }
    )
    result = evaluate_final_gate(**routine)
    assert result["ok"] is True
    assert all(result["checks"].values())


def test_acceptance_defaults_do_not_treat_routine_scale_as_final_closeout():
    routine = _green_kwargs()
    routine.update(
        {
            "cold_requested": ROUTINE_COLD_ATTACHES,
            "cold_successful": ROUTINE_COLD_ATTACHES,
            "recovery_requested": ROUTINE_RECOVERY_CYCLES,
            "recovery_successful": ROUTINE_RECOVERY_CYCLES,
        }
    )
    result = evaluate_final_gate(**routine)
    assert result["ok"] is False
    assert result["checks"]["cold_attach_target"] is False
    assert result["checks"]["recovery_cycle_target"] is False


def test_final_gate_rejects_latency_budget_burn():
    failing = _green_kwargs()
    failing["cold_initialize_catalog_p95_ms"] = 5000.001
    assert evaluate_final_gate(**failing)["ok"] is False


def test_final_gate_latency_summary_is_deterministic():
    assert percentile([1, 2, 3, 4]) == 4.0
    assert latency_summary([4, 1, 2, 3]) == {"count": 4, "p50_ms": 2.0, "p95_ms": 4.0, "max_ms": 4.0}


def test_final_gate_public_shape_does_not_use_forbidden_target_key():
    assert contains_forbidden({"thresholds": {"core_tools": 12}}) is False
    assert contains_forbidden({"targets": {"core_tools": 12}}) is True
