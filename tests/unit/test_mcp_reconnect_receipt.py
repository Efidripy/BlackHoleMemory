from __future__ import annotations

from blackholememory.mcp_reconnect_receipt import build_mcp_reconnect_receipt


def _base(*, state: str = "detached", transport_ready: bool = True) -> dict:
    return {
        "connected": {"state": state},
        "catalog": {"state": "ready" if state == "attached" else "unverified"},
        "runtime": {"state": "healthy"},
        "schema_drift": {"state": "none"},
        "rest_degraded": {"transport_ready": transport_ready},
        "http_sessions": {
            "sessions": [],
            "expired_count": 0,
        },
    }


def test_attached_receipt_is_truthful_about_native_attach():
    payload = _base(state="attached")
    payload["http_sessions"]["sessions"] = [
        {
            "protocol_version": "2025-06-18",
            "catalog_hash": "catalog-a",
            "contract_digest": "contract-a",
            "lease_remaining_seconds": 120.0,
            "contract_state": "aligned",
        }
    ]
    result = build_mcp_reconnect_receipt(**payload)

    assert result["schema_version"] == "bhm.mcp.reconnect-receipt.v1"
    assert result["status"] == "pass"
    assert result["state"] == "attached"
    assert result["action"] == "reuse_session"
    assert result["native_client_attach_proven"] is False
    assert result["automatic_client_reconnect"] is False
    assert result["lease"]["remaining_seconds_min"] == 120.0
    assert result["deterministic_digest"]


def test_expired_receipt_requires_reinitialize_without_secret_data():
    payload = _base()
    payload["http_sessions"]["expired_count"] = 2
    result = build_mcp_reconnect_receipt(**payload)

    assert result["status"] == "gap"
    assert result["state"] == "expired"
    assert result["action"] == "reinitialize"
    assert result["lease"]["expired_count"] == 2
    assert "session_id" not in str(result).casefold()
    assert "token" not in str(result).casefold()


def test_idle_detached_transport_is_not_misreported_as_expired():
    result = build_mcp_reconnect_receipt(**_base())

    assert result["status"] == "gap"
    assert result["state"] == "detached"
    assert result["action"] == "native_probe"


def test_drift_is_blocked_before_reconnect():
    payload = _base(state="attached")
    payload["catalog"] = {"state": "ready"}
    payload["schema_drift"] = {"state": "detected"}
    payload["http_sessions"]["sessions"] = [{"contract_state": "drifted"}]
    result = build_mcp_reconnect_receipt(**payload)

    assert result["status"] == "blocked"
    assert result["state"] == "drifted"
    assert result["action"] == "contract_drift_review"


def test_unhealthy_detached_transport_requires_runtime_repair():
    payload = _base(transport_ready=False)
    payload["runtime"] = {"state": "degraded"}
    result = build_mcp_reconnect_receipt(**payload)

    assert result["status"] == "blocked"
    assert result["action"] == "runtime_restart_required"
