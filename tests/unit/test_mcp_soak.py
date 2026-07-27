from __future__ import annotations

import pytest

from blackholememory.mcp_soak import bounded_clients
from blackholememory.mcp_soak import bounded_restart_round
from blackholememory.mcp_soak import bounded_rounds
from blackholememory.mcp_soak import contains_forbidden
from blackholememory.mcp_soak import lease_wave_invariants
from blackholememory.mcp_soak import reconnect_budget
from blackholememory.mcp_soak import telemetry_storm_invariants


def test_soak_capacity_and_restart_bounds_are_hard_limited():
    assert bounded_clients(10) == 10
    assert bounded_rounds(3) == 3
    assert bounded_restart_round(2, rounds=3) == 2
    assert bounded_restart_round(0, rounds=3) is None
    with pytest.raises(ValueError):
        bounded_clients(11)
    with pytest.raises(ValueError):
        bounded_rounds(6)
    with pytest.raises(ValueError):
        bounded_restart_round(4, rounds=3)


def test_lease_wave_requires_unique_cross_session_identity():
    rows = [
        {
            "client_id": "client-a",
            "session_id": "session-a",
            "process_ownership": {
                "ownership_id": "owner-a",
                "client_id": "client-a",
                "session_id": "session-a",
            },
        },
        {
            "client_id": "client-b",
            "session_id": "session-b",
            "process_ownership": {
                "ownership_id": "owner-b",
                "client_id": "client-b",
                "session_id": "session-b",
            },
        },
    ]
    assert lease_wave_invariants(rows, expected_client_ids=["client-a", "client-b"], expected_count=2)["ok"] is True
    rows[1]["process_ownership"]["client_id"] = "client-a"
    assert lease_wave_invariants(rows, expected_client_ids=["client-a", "client-b"], expected_count=2)["ok"] is False


def test_telemetry_budget_rejects_storm_signatures_and_forbidden_payloads():
    budget = reconnect_budget(clients=10, rounds=3)
    healthy = {
        "totals": {"attempts": budget["max_attempts"], "failures": 0, "timeouts": 0, "fallback_uses": 0},
        "groups": [],
    }
    assert telemetry_storm_invariants(healthy, clients=10, rounds=3)["ok"] is True
    storm = {
        "totals": {"attempts": 1, "failures": 1, "timeouts": 0, "fallback_uses": 0},
        "groups": [{"stage": "reconnect", "outcome": "failure", "error_code": "reconnect_circuit_open", "failures": 1}],
    }
    assert telemetry_storm_invariants(storm, clients=10, rounds=3)["ok"] is False
    assert contains_forbidden({"lease_token": "redacted"}) is True
    assert contains_forbidden({"status": "healthy", "count": 3}) is False
