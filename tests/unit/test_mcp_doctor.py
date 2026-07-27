from __future__ import annotations

from blackholememory.mcp_doctor import choose_next_action
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES


def _state(**overrides):
    base = {
        "runtime": {
            "reachable": True,
            "ready": True,
            "cutover": True,
            "slo_ok": True,
            "projection_pending": 0,
            "outbox_pending": 0,
        },
        "configured": {"status": "aligned", "writes_live_state": False},
        "pipe": {"connected": True},
        "protocol": {"ok": True, "catalog": {"usable": True, "tool_count": len(CORE_TOOL_NAMES)}},
        "leases": {"status": "detached", "pending_count": 0},
        "ownership": {"status": "clean", "invalid_record_count": 0, "orphaned_count": 0},
        "duplicates": {"status": "clean"},
    }
    for key, value in overrides.items():
        base[key].update(value)
    return base


def test_doctor_next_action_prioritizes_runtime_slo_before_retained_duplicates():
    state = _state(
        runtime={"slo_ok": False, "projection_pending": 2},
        duplicates={"status": "retained_duplicates"},
    )

    action = choose_next_action(**state)

    assert action == {
        "severity": "high",
        "reason_code": "runtime_slo_breached",
        "action": "drain the authoritative projection outbox, then rerun MCP Doctor",
    }


def test_doctor_fails_closed_on_duplicate_truth():
    state = _state(duplicates={"status": "active_conflict"})

    action = choose_next_action(**state)

    assert action["severity"] == "high"
    assert action["reason_code"] == "active_duplicate_registration"


def test_doctor_fails_closed_on_active_duplicate_before_claiming_healthy():
    state = _state(duplicates={"status": "active_conflict"})

    action = choose_next_action(**state)

    assert action["severity"] == "high"
    assert action["reason_code"] == "active_duplicate_registration"


def test_doctor_catalog_gate_requires_exact_core_tool_count():
    state = _state(protocol={"ok": True, "catalog": {"usable": True, "tool_count": 11}})

    action = choose_next_action(**state)

    assert action["reason_code"] == "catalog_unusable"


def test_doctor_ownership_probe_is_never_promoted_to_broad_kill():
    state = _state(ownership={"status": "clean", "invalid_record_count": 0, "orphaned_count": 0})

    action = choose_next_action(**state)

    assert "kill" not in action["action"].casefold()
