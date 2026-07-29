"""Validate the canonical BHM Streamable HTTP MCP lifecycle and recovery gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES

SCHEMA_VERSION = "bhm.mcp.streamable-http-validation.v1"
PROTOCOL_VERSION = "2025-06-18"
EXPECTED_TOOL_COUNT = len(CORE_TOOL_NAMES)


def _caller_token() -> str:
    token = os.getenv("BHM_CALLER_TOKEN", "").strip()
    if token or os.name != "nt":
        return token
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, "BHM_CALLER_TOKEN")
    except (ImportError, FileNotFoundError, OSError):
        return ""
    return str(value or "").strip()


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(max(int(round((len(ordered) - 1) * fraction)), 0), len(ordered) - 1)
    return round(ordered[index], 3)


class Probe:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoint = f"{self.base_url}/mcp"
        self.client = httpx.Client(timeout=timeout_seconds, follow_redirects=True)
        self.caller_token = _caller_token()
        if len(self.caller_token) < 32:
            raise RuntimeError("BHM_CALLER_TOKEN is unavailable; initialize the caller credential before validation")
        self.request_id = 0

    def close(self) -> None:
        self.client.close()

    def _next_id(self) -> int:
        self.request_id += 1
        return self.request_id

    def _headers(self, session_id: str = "") -> dict[str, str]:
        headers = {
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {self.caller_token}",
            "content-type": "application/json",
        }
        if session_id:
            headers["mcp-session-id"] = session_id
            headers["mcp-protocol-version"] = PROTOCOL_VERSION
        return headers

    def post(self, method: str, params: dict[str, Any], *, session_id: str = "") -> httpx.Response:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not method.startswith("notifications/"):
            message["id"] = self._next_id()
        return self.client.post(self.endpoint, headers=self._headers(session_id), json=message)

    def initialize(self, *, client_name: str) -> tuple[str, dict[str, Any], float]:
        started = time.perf_counter()
        response = self.post(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": "1.7.1"},
            },
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        response.raise_for_status()
        payload = response.json()
        session_id = response.headers.get("mcp-session-id", "")
        if not session_id or payload.get("result", {}).get("protocolVersion") != PROTOCOL_VERSION:
            raise RuntimeError("initialize contract mismatch")
        notification = self.post("notifications/initialized", {}, session_id=session_id)
        if notification.status_code != 202:
            raise RuntimeError(f"initialized notification failed: {notification.status_code}")
        return session_id, payload, elapsed_ms

    def list_tools(self, session_id: str) -> tuple[list[dict[str, Any]], float]:
        started = time.perf_counter()
        response = self.post("tools/list", {}, session_id=session_id)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        response.raise_for_status()
        tools = response.json().get("result", {}).get("tools")
        if not isinstance(tools, list) or len(tools) != EXPECTED_TOOL_COUNT:
            raise RuntimeError("tools/list did not return the exact core catalog")
        return tools, elapsed_ms

    def call_health(self, session_id: str) -> dict[str, Any]:
        response = self.post(
            "tools/call",
            {"name": "bhm_health", "arguments": {}},
            session_id=session_id,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error") or payload.get("result", {}).get("isError") is True:
            raise RuntimeError("bhm_health MCP call failed")
        return payload

    def delete(self, session_id: str) -> int:
        response = self.client.delete(
            self.endpoint,
            headers={
                "authorization": f"Bearer {self.caller_token}",
                "mcp-session-id": session_id,
                "mcp-protocol-version": PROTOCOL_VERSION,
            },
        )
        return response.status_code


def run(base_url: str, *, cold: int, recovery: int, timeout_seconds: float) -> dict[str, Any]:
    probe = Probe(base_url, timeout_seconds)
    baseline_response = probe.client.get(
        f"{probe.base_url}/bhm/mcp/http/status",
        headers={"authorization": f"Bearer {probe.caller_token}"},
    )
    baseline_response.raise_for_status()
    baseline_status = baseline_response.json().get("sessions", {})
    baseline_session_count = int(baseline_status.get("session_count") or 0)
    initialize_ms: list[float] = []
    catalog_ms: list[float] = []
    catalog_hashes: set[str] = set()
    tool_names: list[str] = []
    cold_successful = 0
    recovery_successful = 0
    health_calls = 0
    failures: list[dict[str, Any]] = []
    try:
        for index in range(cold):
            session_id = ""
            try:
                session_id, _initialize, init_ms = probe.initialize(client_name=f"BHM cold {index + 1}")
                tools, tools_ms = probe.list_tools(session_id)
                initialize_ms.append(init_ms)
                catalog_ms.append(tools_ms)
                catalog_hashes.add(_canonical_hash(tools))
                current_names = [str(item.get("name") or "") for item in tools]
                if not tool_names:
                    tool_names = current_names
                if current_names != tool_names or len(set(current_names)) != EXPECTED_TOOL_COUNT:
                    raise RuntimeError("catalog name/order drift")
                if index in {0, cold - 1}:
                    probe.call_health(session_id)
                    health_calls += 1
                if probe.delete(session_id) not in {200, 202, 204}:
                    raise RuntimeError("session delete failed")
                session_id = ""
                cold_successful += 1
            except Exception as exc:  # noqa: BLE001 - bounded evidence surface
                failures.append({"stage": "cold", "cycle": index + 1, "error": type(exc).__name__})
                if session_id:
                    probe.delete(session_id)

        for index in range(recovery):
            first_session = ""
            second_session = ""
            try:
                first_session, _initialize, _ = probe.initialize(client_name=f"BHM recovery {index + 1}")
                probe.list_tools(first_session)
                if probe.delete(first_session) not in {200, 202, 204}:
                    raise RuntimeError("recovery delete failed")
                stale = probe.post("tools/list", {}, session_id=first_session)
                if stale.status_code != 404:
                    raise RuntimeError("terminated session was not rejected with 404")
                first_session = ""
                second_session, _initialize, _ = probe.initialize(client_name=f"BHM recovered {index + 1}")
                tools, _ = probe.list_tools(second_session)
                catalog_hashes.add(_canonical_hash(tools))
                if probe.delete(second_session) not in {200, 202, 204}:
                    raise RuntimeError("recovered session delete failed")
                second_session = ""
                recovery_successful += 1
            except Exception as exc:  # noqa: BLE001 - bounded evidence surface
                failures.append({"stage": "recovery", "cycle": index + 1, "error": type(exc).__name__})
                for session_id in (first_session, second_session):
                    if session_id:
                        probe.delete(session_id)

        origin = probe.client.post(
            probe.endpoint,
            headers={**probe._headers(), "origin": "https://attacker.example"},
            json={
                "jsonrpc": "2.0",
                "id": probe._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "origin-negative", "version": "1"},
                },
            },
        )
        status_response = probe.client.get(
            f"{probe.base_url}/bhm/mcp/http/status",
            headers={"authorization": f"Bearer {probe.caller_token}"},
        )
        status_response.raise_for_status()
        session_status = status_response.json().get("sessions", {})
    finally:
        probe.close()

    checks = {
        "cold_target": cold_successful == cold,
        "recovery_target": recovery_successful == recovery,
        "exact_tool_count": len(tool_names) == EXPECTED_TOOL_COUNT,
        "unique_tool_names": len(set(tool_names)) == EXPECTED_TOOL_COUNT,
        "single_schema_digest": len(catalog_hashes) == 1,
        "bhm_health": health_calls == (2 if cold > 1 else cold),
        "origin_rejected": origin.status_code == 403,
        "sessions_cleaned": int(session_status.get("session_count") or 0) == baseline_session_count,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": all(checks.values()) and not failures,
        "transport": "streamable_http",
        "server_id": "bhm",
        "protocol_version": PROTOCOL_VERSION,
        "cold": {"requested": cold, "successful": cold_successful},
        "recovery": {"requested": recovery, "successful": recovery_successful},
        "tool_count": len(tool_names),
        "tool_names": tool_names,
        "catalog_hashes": sorted(catalog_hashes),
        "health_calls": health_calls,
        "latency_ms": {
            "initialize_p50": round(statistics.median(initialize_ms), 3) if initialize_ms else 0.0,
            "initialize_p95": _percentile(initialize_ms, 0.95),
            "catalog_p50": round(statistics.median(catalog_ms), 3) if catalog_ms else 0.0,
            "catalog_p95": _percentile(catalog_ms, 0.95),
        },
        "checks": checks,
        "failures": failures[:16],
        "session_status": session_status,
        "baseline_session_count": baseline_session_count,
        "writes_live_memory": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cold", type=int, default=25)
    parser.add_argument("--recovery", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    if not 1 <= args.cold <= 100 or not 1 <= args.recovery <= 50:
        raise SystemExit("cold/recovery counts are outside bounded validation limits")
    result = run(
        args.base_url,
        cold=args.cold,
        recovery=args.recovery,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
