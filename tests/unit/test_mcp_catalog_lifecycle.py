from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "validate-bhm-mcp-catalog-lifecycle.py"
SPEC = importlib.util.spec_from_file_location("bhm_mcp_catalog_lifecycle", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_transport_catalog_hash_matches_registry_domain() -> None:
    tools = [{"name": "b", "inputSchema": {"type": "object"}}, {"name": "a"}]
    expected = hashlib.sha256(json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert MODULE._transport_catalog_hash(tools) == expected
    assert MODULE._transport_catalog_hash({"tools": tools}) is None


def test_bounded_lifecycle_probe_proves_stable_digest_and_cleanup(monkeypatch) -> None:
    active: dict[str, Any] | None = None
    sequence = {"value": 0}
    tools = [{"name": "bhm_health", "description": "health", "inputSchema": {"type": "object"}}]

    def fake_status(_base_url: str, _path: str, *, timeout_seconds: float):
        del timeout_seconds
        sessions = [] if active is None else [dict(active)]
        return {"sessions": {"sessions": sessions}}, None

    def fake_request(_url: str, message: dict[str, Any] | None, *, timeout_seconds: float, session_id: str = "", method: str = "POST"):
        del timeout_seconds, session_id
        nonlocal active
        if method == "DELETE":
            active = None
            return 200, {}, {}
        assert message is not None
        request_method = str(message.get("method") or "")
        if request_method == "initialize":
            sequence["value"] += 1
            token = f"session-{sequence['value']}"
            session_ref = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
            active = {"session_ref": session_ref, "state": "initialized", "catalog_hash": None}
            return (
                200,
                {"result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "bhm", "version": "test", "surface": "core"}}},
                {"Mcp-Session-Id": token},
            )
        if request_method == "notifications/initialized":
            return 202, {}, {}
        if request_method == "tools/list":
            assert active is not None
            active["state"] = "catalog_ready"
            active["catalog_hash"] = MODULE._transport_catalog_hash(tools)
            return 200, {"result": {"tools": tools}}, {}
        raise AssertionError(request_method)

    monkeypatch.setattr(MODULE, "_get_json", fake_status)
    monkeypatch.setattr(MODULE, "_http_mcp_request", fake_request)
    monkeypatch.setattr(MODULE, "CORE_TOOL_NAMES", frozenset({"bhm_health"}))
    report = MODULE.validate_catalog_lifecycle(base_url="http://127.0.0.1:8000", probes=2, timeout_seconds=2)

    assert report["ok"] is True
    assert report["checks"]["schema_hash_stable"] is True
    assert report["checks"]["transport_catalog_hash_stable"] is True
    assert report["checks"]["registry_hash_bound"] is True
    assert report["checks"]["ephemeral_cleanup"] is True
    assert report["catalog"]["digest_domains_distinct"] is True
    assert report["issues"] == []


def test_probe_limit_is_bounded() -> None:
    try:
        MODULE.validate_catalog_lifecycle(probes=MODULE.MAX_PROBES + 1)
    except ValueError as exc:
        assert "between 1 and 25" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("probe limit was not enforced")
