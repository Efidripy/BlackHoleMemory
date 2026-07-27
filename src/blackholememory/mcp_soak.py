"""Pure policy helpers for the bounded P18.18 MCP multi-session soak.

The live validator owns processes and transport I/O.  This module keeps the
capacity, identity, privacy and reconnect-budget decisions deterministic and
unit-testable without touching runtime state.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


SCHEMA_VERSION = "bhm.mcp.multi-session-soak.v1"
MAX_CLIENTS = 10
DEFAULT_CLIENTS = 10
DEFAULT_ROUNDS = 3
MAX_ROUNDS = 5
DEFAULT_RESTART_ROUND = 2
MAX_RESTARTS = 1
EXPECTED_TOOL_COUNT = 12

FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "content",
        "tool_arguments",
        "arguments",
        "environment",
        "env",
        "secret",
        "secrets",
        "token",
        "lease_token",
        "session_id",
        "connection_id",
        "ownership_id",
        "target",
        "targets",
        "path",
        "command",
        "args",
        "pid",
        "pids",
    }
)


def bounded_clients(value: int) -> int:
    """Validate the hard concurrency ceiling used by MCP timeout contract."""

    clients = int(value)
    if not 1 <= clients <= MAX_CLIENTS:
        raise ValueError(f"clients must be between 1 and {MAX_CLIENTS}")
    return clients


def bounded_rounds(value: int) -> int:
    rounds = int(value)
    if not 1 <= rounds <= MAX_ROUNDS:
        raise ValueError(f"rounds must be between 1 and {MAX_ROUNDS}")
    return rounds


def bounded_restart_round(value: int | None, *, rounds: int) -> int | None:
    if value is None or int(value) == 0:
        return None
    restart_round = int(value)
    if not 1 <= restart_round <= rounds:
        raise ValueError("restart_round must be within the soak rounds")
    return restart_round


def session_budget(*, clients: int, rounds: int) -> dict[str, int]:
    clients = bounded_clients(clients)
    rounds = bounded_rounds(rounds)
    return {
        "clients_per_wave": clients,
        "waves": rounds,
        "sessions": clients * rounds,
        "max_active_leases": clients,
    }


def reconnect_budget(*, clients: int, rounds: int) -> dict[str, int]:
    """Return a deliberately generous, finite retry budget.

    A clean soak allows one bounded retry per session plus a small fixed margin
    for the forced runtime restart.  Circuit-open, timeout, fallback and
    identity failures remain hard failures regardless of this budget.
    """

    budget = session_budget(clients=clients, rounds=rounds)
    sessions = budget["sessions"]
    return {
        "max_attempts": sessions * 4 + 20,
        "max_failures": sessions + clients + 4,
        "max_broken_pipe_failures": clients + 4,
    }


def _values(rows: Iterable[dict[str, Any]], key: str) -> list[str]:
    return [str(row.get(key) or "") for row in rows if isinstance(row, dict)]


def lease_wave_invariants(
    leases: Iterable[dict[str, Any]],
    *,
    expected_client_ids: Iterable[str],
    expected_count: int,
) -> dict[str, Any]:
    """Check one active lease wave without returning identifiers."""

    rows = [row for row in leases if isinstance(row, dict)]
    expected = {str(value) for value in expected_client_ids}
    clients = _values(rows, "client_id")
    sessions = _values(rows, "session_id")
    ownership_ids: list[str] = []
    identity_matches = True
    ownership_complete = True
    for row in rows:
        ownership = row.get("process_ownership")
        if not isinstance(ownership, dict):
            ownership_complete = False
            continue
        ownership_ids.append(str(ownership.get("ownership_id") or ""))
        identity_matches = identity_matches and (
            ownership.get("client_id") == row.get("client_id")
            and ownership.get("session_id") == row.get("session_id")
        )
    actual = set(clients)
    return {
        "expected_count": expected_count,
        "observed_count": len(rows),
        "count_exact": len(rows) == expected_count,
        "client_set_exact": actual == expected,
        "client_ids_unique": len(clients) == len(set(clients)),
        "session_ids_unique": len(sessions) == len(set(sessions)),
        "ownership_ids_unique": len(ownership_ids) == len(set(ownership_ids)),
        "ownership_complete": ownership_complete and len(ownership_ids) == len(rows),
        "ownership_identity_matches": identity_matches,
        "max_active_leases_ok": len(rows) <= MAX_CLIENTS,
        "ok": bool(
            len(rows) == expected_count
            and actual == expected
            and len(clients) == len(set(clients))
            and len(sessions) == len(set(sessions))
            and len(ownership_ids) == len(set(ownership_ids))
            and ownership_complete
            and identity_matches
            and len(rows) <= MAX_CLIENTS
        ),
    }


def detached_invariants(leases: Iterable[dict[str, Any]], expected_client_ids: Iterable[str]) -> dict[str, Any]:
    expected = {str(value) for value in expected_client_ids}
    rows = [row for row in leases if isinstance(row, dict)]
    leaked = {str(row.get("client_id") or "") for row in rows} & expected
    return {
        "active_leases": len(rows),
        "expected_clients_absent": not leaked,
        "all_leases_detached": not rows,
        "ok": not rows and not leaked,
    }


def telemetry_storm_invariants(
    telemetry: dict[str, Any],
    *,
    clients: int,
    rounds: int,
) -> dict[str, Any]:
    """Classify aggregate telemetry against the bounded reconnect policy."""

    budget = reconnect_budget(clients=clients, rounds=rounds)
    totals = telemetry.get("totals") if isinstance(telemetry.get("totals"), dict) else {}
    groups = telemetry.get("groups") if isinstance(telemetry.get("groups"), list) else []
    errors: dict[str, int] = {}
    stage_failures = 0
    connection_attempts = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        code = str(group.get("error_code") or "none")
        failures = int(group.get("failures") or 0)
        errors[code] = errors.get(code, 0) + failures
        stage_failures += failures
        if str(group.get("stage") or "") in {"startup", "api_probe", "pipe_connect", "reconnect"}:
            connection_attempts += int(group.get("attempts") or 0)
    hard_failure_codes = {
        "invalid_request",
        "unknown",
        "timeout",
        "connect_timeout",
        "capacity_exhausted",
        "lease_expired",
        "lease_unauthorized",
        "transport_binding_mismatch",
        "process_ownership_mismatch",
        "reconnect_circuit_open",
        "fallback_grace",
        "api_unavailable",
        "catalog_unusable",
    }
    hard_failures = sum(errors.get(code, 0) for code in hard_failure_codes)
    broken_pipe_failures = errors.get("broken_pipe", 0)
    attempts = int(totals.get("attempts") or 0)
    failures = int(totals.get("failures") or 0)
    timeouts = int(totals.get("timeouts") or 0)
    fallback_uses = int(totals.get("fallback_uses") or 0)
    return {
        "attempts": attempts,
        "connection_attempts": connection_attempts,
        "failures": failures,
        "timeouts": timeouts,
        "fallback_uses": fallback_uses,
        "hard_failures": hard_failures,
        "broken_pipe_failures": broken_pipe_failures,
        "stage_failures": stage_failures,
        "max_attempts": budget["max_attempts"],
        "max_failures": budget["max_failures"],
        "max_broken_pipe_failures": budget["max_broken_pipe_failures"],
        "attempts_bounded": connection_attempts <= budget["max_attempts"],
        "failures_bounded": failures <= budget["max_failures"],
        "hard_failures_zero": hard_failures == 0,
        "timeouts_zero": timeouts == 0,
        "fallbacks_zero": fallback_uses == 0,
        "broken_pipe_bounded": broken_pipe_failures <= budget["max_broken_pipe_failures"],
        "ok": bool(
            connection_attempts <= budget["max_attempts"]
            and failures <= budget["max_failures"]
            and hard_failures == 0
            and timeouts == 0
            and fallback_uses == 0
            and broken_pipe_failures <= budget["max_broken_pipe_failures"]
        ),
    }


def contains_forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).casefold() in FORBIDDEN_KEYS or contains_forbidden(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_forbidden(item) for item in value)
    return False


__all__ = [
    "DEFAULT_CLIENTS",
    "DEFAULT_RESTART_ROUND",
    "DEFAULT_ROUNDS",
    "EXPECTED_TOOL_COUNT",
    "MAX_CLIENTS",
    "MAX_RESTARTS",
    "MAX_ROUNDS",
    "SCHEMA_VERSION",
    "bounded_clients",
    "bounded_restart_round",
    "bounded_rounds",
    "contains_forbidden",
    "detached_invariants",
    "lease_wave_invariants",
    "reconnect_budget",
    "session_budget",
    "telemetry_storm_invariants",
]
