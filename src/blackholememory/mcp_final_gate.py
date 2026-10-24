"""Policy helpers for the P18.20 MCP reliability profiles.

The live validator owns process and HTTP I/O. This module keeps routine and
final-acceptance thresholds plus the aggregate decision deterministic and
unit-testable.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from .mcp_surfaces import CORE_TOOL_NAMES


SCHEMA_VERSION = "bhm.mcp.final-gate.v1"
ROUTINE_COLD_ATTACHES = 25
ROUTINE_RECOVERY_CYCLES = 10
REQUIRED_COLD_ATTACHES = 100
REQUIRED_RECOVERY_CYCLES = 50
EXPECTED_CLIENTS = ("codex", "claude")
# Keep policy thresholds bound to the live canonical core catalog.  The P26
# additive code-intelligence surface expanded it beyond the historical 12.
EXPECTED_TOOL_COUNT = len(CORE_TOOL_NAMES)
COLD_INITIALIZE_CATALOG_P95_MS = 5_000.0
RECOVERY_ATTACH_AFTER_READY_P95_MS = 15_000.0


def percentile(values: Iterable[float], quantile: float = 0.95) -> float | None:
    """Return a bounded nearest-rank percentile without interpolation drift."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if not 0.0 < float(quantile) <= 1.0:
        raise ValueError("quantile must be within (0, 1]")
    rank = max(1, math.ceil(len(ordered) * float(quantile)))
    return round(ordered[min(rank, len(ordered)) - 1], 3)


def latency_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    ordered = sorted(float(value) for value in values)
    return {
        "count": len(ordered),
        "p50_ms": percentile(ordered, 0.50),
        "p95_ms": percentile(ordered, 0.95),
        "max_ms": round(max(ordered), 3) if ordered else None,
    }


def evaluate_final_gate(
    *,
    cold_requested: int,
    cold_successful: int,
    cold_catalog_missing: int,
    cold_initialize_catalog_p95_ms: float | None,
    recovery_requested: int,
    recovery_successful: int,
    recovery_restart_failures: int,
    recovery_catalog_missing: int,
    recovery_attach_after_ready_p95_ms: float | None,
    matrix_ok: bool,
    matrix_client_count: int,
    current_client_proof_ok: bool,
    config_canary_ok: bool,
    config_rollback_ok: bool,
    config_live_check_ok: bool,
    config_targets_unchanged: bool,
    active_duplicate_registrations: int,
    retained_residue_explicit: bool,
    unauthorized_fallbacks_or_writes: int,
    telemetry_fallback_uses: int,
    runtime_healthy_before: bool,
    runtime_healthy_after: bool,
    slo_healthy_before: bool,
    slo_healthy_after: bool,
    outbox_pending_after: int,
    outbox_failed_after: int,
    outbox_dead_letter_after: int,
    active_leases_after: int,
    owned_processes_after: int,
    ownership_records_clean: bool,
    writes_live_state: bool,
    required_cold_attaches: int = REQUIRED_COLD_ATTACHES,
    required_recovery_cycles: int = REQUIRED_RECOVERY_CYCLES,
) -> dict[str, Any]:
    """Evaluate every P18.20 profile invariant and expose each check."""

    if int(required_cold_attaches) < 1 or int(required_recovery_cycles) < 1:
        raise ValueError("required attach and recovery thresholds must be positive")

    checks = {
        "cold_attach_target": int(cold_requested) >= int(required_cold_attaches),
        "cold_attach_all_successful": int(cold_successful) == int(cold_requested),
        "cold_catalog_complete": int(cold_catalog_missing) == 0,
        "cold_initialize_catalog_p95_within_budget": (
            cold_initialize_catalog_p95_ms is not None
            and float(cold_initialize_catalog_p95_ms) <= COLD_INITIALIZE_CATALOG_P95_MS
        ),
        "recovery_cycle_target": int(recovery_requested) >= int(required_recovery_cycles),
        "recovery_all_successful": (
            int(recovery_successful) == int(recovery_requested)
            and int(recovery_restart_failures) == 0
        ),
        "recovery_catalog_complete": int(recovery_catalog_missing) == 0,
        "recovery_attach_after_ready_p95_within_budget": (
            recovery_attach_after_ready_p95_ms is not None
            and float(recovery_attach_after_ready_p95_ms) <= RECOVERY_ATTACH_AFTER_READY_P95_MS
        ),
        "client_matrix": bool(matrix_ok) and int(matrix_client_count) == len(EXPECTED_CLIENTS),
        "current_client_exact_core": bool(current_client_proof_ok),
        "config_canary": bool(config_canary_ok),
        "config_rollback": bool(config_rollback_ok),
        "config_live_check": bool(config_live_check_ok),
        "config_targets_unchanged": bool(config_targets_unchanged),
        "active_duplicates_zero": int(active_duplicate_registrations) == 0,
        "retained_residue_explicit": bool(retained_residue_explicit),
        "unauthorized_fallbacks_writes_zero": int(unauthorized_fallbacks_or_writes) == 0,
        "telemetry_fallback_zero": int(telemetry_fallback_uses) == 0,
        "runtime_healthy_before": bool(runtime_healthy_before),
        "runtime_healthy_after": bool(runtime_healthy_after),
        "slo_healthy_before": bool(slo_healthy_before),
        "slo_healthy_after": bool(slo_healthy_after),
        "outbox_pending_zero": int(outbox_pending_after) == 0,
        "outbox_failed_zero": int(outbox_failed_after) == 0,
        "outbox_dead_letter_zero": int(outbox_dead_letter_after) == 0,
        "active_leases_zero": int(active_leases_after) == 0,
        "owned_processes_zero": int(owned_processes_after) == 0,
        "ownership_records_clean": bool(ownership_records_clean),
        "no_live_state_writes": not bool(writes_live_state),
    }
    return {"schema_version": SCHEMA_VERSION, "checks": checks, "ok": all(checks.values())}


__all__ = [
    "EXPECTED_CLIENTS",
    "EXPECTED_TOOL_COUNT",
    "COLD_INITIALIZE_CATALOG_P95_MS",
    "RECOVERY_ATTACH_AFTER_READY_P95_MS",
    "ROUTINE_COLD_ATTACHES",
    "ROUTINE_RECOVERY_CYCLES",
    "REQUIRED_COLD_ATTACHES",
    "REQUIRED_RECOVERY_CYCLES",
    "SCHEMA_VERSION",
    "evaluate_final_gate",
    "latency_summary",
    "percentile",
]
