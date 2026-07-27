from __future__ import annotations

from blackholememory.unified_mcp_contract import build_unified_mcp_contract
from blackholememory.unified_mcp_contract import verify_unified_mcp_contract_digest


def test_unified_contract_is_deterministic_and_truthful_when_native_detached():
    native = {"attached": False, "current_session_verified": False, "runtime_lease_live": False, "reason_code": "no_live_native_lease"}
    first = build_unified_mcp_contract(native_mcp=native)
    second = build_unified_mcp_contract(native_mcp=native)
    assert first["contract_digest"] == second["contract_digest"]
    assert verify_unified_mcp_contract_digest(first)
    assert first["degraded_mode"]["status"] == "MCP unavailable"
    assert first["execution"]["client_files_written"] is False


def test_unified_contract_fails_closed_on_schema_drift():
    baseline = build_unified_mcp_contract()
    schema_hash = baseline["catalog"]["schema_hash"]
    result = build_unified_mcp_contract(
        client_snapshots=[
            {"client": "codex", "server_id": "bhm", "schema_hash": "drift", "status": "degraded", "rest_bridge": True},
            {"client": "claude", "server_id": "bhm", "schema_hash": schema_hash, "status": "degraded", "rest_bridge": True},
        ]
    )
    assert any(issue["code"] == "schema_hash_mismatch" for issue in result["issues"])
    assert result["checks"]["client_matrix_aligned"] is False
