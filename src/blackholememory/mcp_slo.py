"""Pure P18.19 MCP SLO and error-budget policy.

The live validator owns process/runtime probes.  This module keeps target
values, percentile math, registration classification and fail-closed budget
decisions deterministic and unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


SCHEMA_VERSION = "bhm.mcp.slo-error-budget.v1"
TARGETS = {
    "cold_initialize_catalog_p95_ms": 5_000.0,
    "recovery_attach_after_ready_p95_ms": 15_000.0,
    "active_duplicate_registrations": 0,
    "indefinite_no_catalog_states": 0,
    "unauthorized_fallbacks_or_writes": 0,
}

# P18.2 evidence is retained as the historical comparison point.  It is not
# silently replaced by today's measurement and does not include P18.20 scale.
P18_2_BASELINE = {
    "schema_version": "bhm.mcp.attach-benchmark.v1",
    "cold_total_p95_ms": 2_219.491,
    "warm_total_p95_ms": 3_757.267,
    "initialize_p95_ms": 668.442,
    "catalog_p95_ms": 10.965,
    "tool_call_p95_ms": 3_086.807,
    "retries": 0,
    "successful_runs": 2,
}


def percentile(values: Iterable[float], percentile_value: float = 95.0) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if percentile_value <= 0:
        return ordered[0]
    if percentile_value >= 100:
        return ordered[-1]
    rank = (percentile_value / 100.0) * len(ordered)
    index = max(0, min(len(ordered) - 1, int(rank + 0.999999) - 1))
    return round(ordered[index], 3)


def classify_registration_inventory(payload: dict[str, Any]) -> dict[str, Any]:
    """Separate active conflicts from retained generated-cache residue.

    P18.12 deliberately retained old cache/plugin copies and documented them;
    treating those artifacts as active runtime registrations would make the SLO
    lie in the opposite direction.  Any canonical drift, unrecognized surface
    or wrong active count remains a hard active conflict.
    """

    valid_payload = isinstance(payload, dict) and isinstance(payload.get("issues"), list) and "registration_count" in payload
    issues = [item for item in payload.get("issues") or [] if isinstance(item, dict)]
    active_codes = {
        "canonical_registration_count",
        "canonical_fingerprint_drift",
        "unrecognized_bhm_surface",
    }
    retained_codes: set[str] = set()
    active = [item for item in issues if item.get("code") in active_codes]
    retained = [item for item in issues if item.get("code") in retained_codes]
    unknown = [item for item in issues if item.get("code") not in active_codes | retained_codes]
    return {
        "active_duplicate_registrations": len(active),
        "active_conflict_codes": sorted({str(item.get("code")) for item in active}),
        "retained_cache_residue": len(retained),
        "retained_residue_codes": sorted({str(item.get("code")) for item in retained}),
        "unknown_issue_count": len(unknown),
        "inventory_available": valid_payload,
        "active_target_ok": valid_payload and not active and not unknown,
        "retained_residue_explicit": all(item.get("code") in retained_codes for item in retained),
    }


def evaluate_slo(
    *,
    cold_initialize_catalog_ms: Iterable[float],
    recovery_attach_ms: Iterable[float],
    catalog_missing_states: int,
    active_duplicate_registrations: int,
    unauthorized_fallbacks_or_writes: int,
    samples: int,
    successful_samples: int,
    retry_count: int,
    registration: dict[str, Any],
    adapter_check_ok: bool,
    runtime_healthy_before: bool,
    runtime_healthy_after: bool,
    telemetry_fallback_uses: int,
    writes_live_state: bool,
    slo_healthy_before: bool,
    slo_healthy_after: bool,
    outbox_failed_before: int,
    outbox_failed_after: int,
    outbox_pending_after: int,
    outbox_dead_letter_after: int,
) -> dict[str, Any]:
    cold_p95 = percentile(cold_initialize_catalog_ms)
    recovery_p95 = percentile(recovery_attach_ms)
    checks = {
        "cold_initialize_catalog_p95_within_budget": cold_p95 is not None and cold_p95 <= TARGETS["cold_initialize_catalog_p95_ms"],
        "recovery_attach_after_ready_p95_within_budget": recovery_p95 is not None and recovery_p95 <= TARGETS["recovery_attach_after_ready_p95_ms"],
        "active_duplicate_registrations_zero": active_duplicate_registrations == TARGETS["active_duplicate_registrations"],
        "indefinite_no_catalog_states_zero": catalog_missing_states == TARGETS["indefinite_no_catalog_states"],
        "unauthorized_fallbacks_or_writes_zero": unauthorized_fallbacks_or_writes == TARGETS["unauthorized_fallbacks_or_writes"],
        "registration_active_target_green": registration.get("active_target_ok") is True,
        "registration_residue_explicit": registration.get("retained_residue_explicit") is True,
        "adapter_check_green": adapter_check_ok,
        "successful_samples_complete": samples > 0 and successful_samples == samples,
        "retries_zero": retry_count == 0,
        "runtime_healthy_before": runtime_healthy_before,
        "runtime_healthy_after": runtime_healthy_after,
        "telemetry_fallbacks_zero": telemetry_fallback_uses == 0,
        "writes_live_state_false": writes_live_state is False,
        "slo_healthy_before": slo_healthy_before,
        "slo_healthy_after": slo_healthy_after,
        "outbox_failed_zero": outbox_failed_after == 0,
        "outbox_failed_unchanged": outbox_failed_before == outbox_failed_after,
        "outbox_pending_zero": outbox_pending_after == 0,
        "outbox_dead_letter_zero": outbox_dead_letter_after == 0,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": all(checks.values()),
        "targets": dict(TARGETS),
        "observed": {
            "cold_initialize_catalog_p95_ms": cold_p95,
            "recovery_attach_after_ready_p95_ms": recovery_p95,
            "active_duplicate_registrations": active_duplicate_registrations,
            "retained_cache_residue": int(registration.get("retained_cache_residue", 0)),
            "indefinite_no_catalog_states": catalog_missing_states,
            "unauthorized_fallbacks_or_writes": unauthorized_fallbacks_or_writes,
            "samples": samples,
            "successful_samples": successful_samples,
            "retry_count": retry_count,
            "telemetry_fallback_uses": telemetry_fallback_uses,
        },
        "registration": registration,
        "checks": checks,
        "error_budget": {
            "attach_failures": max(samples - successful_samples, 0),
            "retries": max(retry_count, 0),
            "catalog_missing": max(catalog_missing_states, 0),
            "unauthorized_fallbacks_or_writes": max(unauthorized_fallbacks_or_writes, 0),
        },
        "baseline": dict(P18_2_BASELINE),
        "writes_live_state": False,
    }


__all__ = [
    "P18_2_BASELINE",
    "SCHEMA_VERSION",
    "TARGETS",
    "classify_registration_inventory",
    "evaluate_slo",
    "percentile",
]
