"""Run the live bounded P18.15 MCP panel gate."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.request
from typing import Any

from bhm_runtime_endpoints import endpoint_url


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.mcp_panel import EXPECTED_CORE_TOOL_COUNT
from blackholememory.mcp_panel import SCHEMA_VERSION
from blackholememory.mcp_final_gate import EXPECTED_CLIENTS
from blackholememory.caller_auth import configured_caller_token
from blackholememory.local_endpoint_policy import open_local_url
from blackholememory.local_endpoint_policy import read_bounded_response
from blackholememory.resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS


VALIDATION_SCHEMA_VERSION = "bhm.mcp.panel-validation.v1"
FORBIDDEN_KEYS = {
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
    "client_id",
    "session_id",
    "connection_id",
    "ownership_id",
}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=endpoint_url("bhm_api"))
    parser.add_argument("--compact", action="store_true")
    return parser.parse_args()


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    token = configured_caller_token()
    if len(token) < 32:
        raise RuntimeError("BHM_CALLER_TOKEN is unavailable")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "BHM-P18.15-Validator/1.7.1",
        },
    )
    with open_local_url(request, timeout=BHM_INTERNAL_HTTP_TIMEOUT_SECONDS) as response:
        status_value = getattr(response, "status", None)
        status = int(status_value if status_value is not None else response.getcode())
        if status != 200:
            raise RuntimeError(f"unexpected HTTP status {status}")
        payload = json.loads(read_bounded_response(response, limit=128 * 1024).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"non-object response from {path}")
    return payload


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                return True
            if _contains_forbidden_key(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def main() -> int:
    args = _args()
    before_slo = _get_json(args.base_url, "/bhm/health/slo")
    http_envelope = _get_json(args.base_url, "/bhm/mcp/http/status")
    panel = _get_json(args.base_url, "/bhm/telemetry/mcp-panel")
    after_slo = _get_json(args.base_url, "/bhm/health/slo")
    configured = panel.get("configured") if isinstance(panel.get("configured"), dict) else {}
    connected = panel.get("connected") if isinstance(panel.get("connected"), dict) else {}
    catalog = panel.get("catalog") if isinstance(panel.get("catalog"), dict) else {}
    runtime = panel.get("runtime") if isinstance(panel.get("runtime"), dict) else {}
    rest_degraded = panel.get("rest_degraded") if isinstance(panel.get("rest_degraded"), dict) else {}
    schema_drift = panel.get("schema_drift") if isinstance(panel.get("schema_drift"), dict) else {}
    overall = panel.get("overall") if isinstance(panel.get("overall"), dict) else {}
    configured_sources = configured.get("sources") if isinstance(configured.get("sources"), list) else []
    configured_clients = {
        str(item.get("client"))
        for item in configured_sources
        if isinstance(item, dict)
    }
    http_sessions = http_envelope.get("sessions") if isinstance(http_envelope.get("sessions"), dict) else {}
    http_ready = http_envelope.get("transport") == "streamable_http" and bool(http_sessions)
    runtime_lease_live = connected.get("state") == "attached" and int(connected.get("attached_count") or 0) > 0
    if runtime_lease_live:
        expected_mode = "attached"
        connection_truth = rest_degraded.get("status") == "native MCP live; current session unverified"
        catalog_truth = (
            (
                catalog.get("state") == "ready"
                and catalog.get("observed_tool_count") == EXPECTED_CORE_TOOL_COUNT
            )
            or (
                catalog.get("state") == "unverified"
                and catalog.get("observed_tool_count") == 0
                and catalog.get("expected_tool_count") == EXPECTED_CORE_TOOL_COUNT
            )
        )
        rest_transport_truth = (
            rest_degraded.get("degraded") is False
            and rest_degraded.get("runtime_lease_live") is True
            and rest_degraded.get("current_session_verified") is False
            and rest_degraded.get("transport_ready") is True
        )
    elif http_ready:
        expected_mode = "ready_detached"
        connection_truth = connected.get("state") == "detached" and int(connected.get("attached_count") or 0) == 0
        catalog_truth = (
            catalog.get("state") == "unverified"
            and catalog.get("observed_tool_count") == 0
            and catalog.get("expected_tool_count") == EXPECTED_CORE_TOOL_COUNT
        )
        rest_transport_truth = (
            rest_degraded.get("status") == "native MCP transport ready; session idle or detached"
            and rest_degraded.get("degraded") is True
            and rest_degraded.get("mcp_available") is False
            and rest_degraded.get("attached") is False
            and rest_degraded.get("current_session_verified") is False
            and rest_degraded.get("transport_ready") is True
            and rest_degraded.get("streamable_http_ready") is True
            and str(rest_degraded.get("recovery_action") or "").startswith("invoke a native BHM tool")
        )
    else:
        expected_mode = "unavailable"
        connection_truth = connected.get("state") == "detached" and int(connected.get("attached_count") or 0) == 0
        catalog_truth = catalog.get("state") == "unverified" and catalog.get("observed_tool_count") == 0
        rest_transport_truth = (
            rest_degraded.get("status") == "MCP unavailable"
            and rest_degraded.get("degraded") is True
            and rest_degraded.get("current_session_verified") is False
            and rest_degraded.get("transport_ready") is False
        )
    false_green_truth = (
        overall.get("false_green_prevented") is False
        if overall.get("state") == "healthy"
        else overall.get("state") != "healthy" and overall.get("false_green_prevented") is True
    )
    expected_schema_drift = "none" if catalog.get("state") == "ready" else "unverified"
    checks = {
        "schema": panel.get("schema_version") == SCHEMA_VERSION,
        "bounded_read_only": panel.get("read_only") is True and panel.get("bounded") is True,
        "no_live_writes": panel.get("writes_live_state") is False,
        "configured_sources": (
            configured.get("state") == "configured"
            and configured.get("source_count") == len(EXPECTED_CLIENTS)
            and configured.get("configured_count") == len(EXPECTED_CLIENTS)
            and configured_clients == set(EXPECTED_CLIENTS)
        ),
        "runtime_healthy": runtime.get("state") == "healthy" and runtime.get("ready") is True and runtime.get("cutover") is True and runtime.get("slo") == "healthy",
        "native_connection_truth": connection_truth,
        "catalog_truth": catalog_truth,
        "rest_transport_truth": rest_transport_truth,
        "recovery_not_blanket_reload": not str(rest_degraded.get("recovery_action") or "").startswith("reload"),
        "schema_drift_truth": schema_drift.get("state") == expected_schema_drift,
        "false_green_prevented": false_green_truth,
        "privacy": not _contains_forbidden_key(panel),
        "slo_after": after_slo.get("status") == "healthy" and int((after_slo.get("observed") or {}).get("outbox", {}).get("failed", 0)) == 0,
        "slo_before_after_no_failed_writes": int((before_slo.get("observed") or {}).get("outbox", {}).get("failed", 0)) == int((after_slo.get("observed") or {}).get("outbox", {}).get("failed", 0)),
    }
    result = {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "ok": all(checks.values()),
        "checks": checks,
        "panel": {
            "expected_mode": expected_mode,
            "overall": overall.get("state"),
            "configured": configured.get("configured_count"),
            "connected": connected.get("state"),
            "catalog": catalog.get("state"),
            "runtime": runtime.get("state"),
            "rest_status": rest_degraded.get("status"),
            "schema_drift": schema_drift.get("state"),
        },
        "http_status": {
            "transport": http_envelope.get("transport"),
            "status": http_sessions.get("status"),
            "attached_count": int(http_sessions.get("attached_count") or 0),
        },
        "slo": {
            "before": before_slo.get("status"),
            "after": after_slo.get("status"),
            "outbox_failed_before": int((before_slo.get("observed") or {}).get("outbox", {}).get("failed", 0)),
            "outbox_failed_after": int((after_slo.get("observed") or {}).get("outbox", {}).get("failed", 0)),
        },
        "writes_live_state": False,
    }
    output = json.dumps(result, ensure_ascii=False, separators=(",", ":")) if args.compact else json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(output)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema_version": VALIDATION_SCHEMA_VERSION, "ok": False, "error_code": type(exc).__name__.casefold()}))
        raise SystemExit(1)
