"""Telemetry polling for the launcher (FIX-017 BL.2): bounded BHM telemetry fetch, local LLM / remote probes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from urllib.parse import urlsplit
import json
import re
import socket
import threading
import time
import urllib.request

from bhm_launcher_readiness import LocalEndpointError
from bhm_launcher_readiness import MAX_HTTP_BYTES
from bhm_launcher_readiness import read_bounded_response
from bhm_runtime_endpoints import endpoint_parts
from blackholememory.resource_limits import LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_TCP_PROBE_TIMEOUT_SECONDS

from bhm_launcher_host import launcher as _launcher


_LAST_TELEMETRY: dict[str, str] = {
    "memory_count": "--",
    "link_count": "--",
    "node_count": "--",
    "sessions": "--",
    "observations": "--",
    "sqlite_state": "--",
    "qdrant_state": "--",
    "provider_state": "--",
    "mcp_state": "--",
    "projection_queue": "--",
    "consolidation_queue": "--",
    "slo_state": "--",
    "last_sys": "--",
}


@dataclass
class LlmInventoryCache:
    next_refresh_at: float = 0.0
    status: _launcher.ServiceStatus | None = None

    def reset(self) -> None:
        self.next_refresh_at = 0.0
        self.status = None


@dataclass
class TelemetryPathState:
    failures: int = 0
    next_attempt_at: float = 0.0
    last_result: _launcher.JsonRequestResult | None = None


class TelemetryRequestState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paths: dict[str, _launcher.TelemetryPathState] = {}

    def should_request(self, path: str, now: float) -> bool:
        with self._lock:
            return now >= self._paths.get(path, _launcher.TelemetryPathState()).next_attempt_at

    def last_result(self, path: str) -> _launcher.JsonRequestResult | None:
        with self._lock:
            state = self._paths.get(path)
            return state.last_result if state else None

    def was_recorded(self, path: str, result: _launcher.JsonRequestResult) -> bool:
        with self._lock:
            state = self._paths.get(path)
            return state is not None and state.last_result is result

    def record_result(self, path: str, result: _launcher.JsonRequestResult, *, error_class: str | None = None) -> None:
        with self._lock:
            state = self._paths.setdefault(path, _launcher.TelemetryPathState())
            if result.ok:
                previous_failures = state.failures
                state.failures = 0
                state.next_attempt_at = 0.0
                state.last_result = result
                if previous_failures:
                    _launcher.append_launcher_log(f"TELEMETRY RECOVERED: path={path}; failures={previous_failures}")
                return

            state.failures += 1
            state.next_attempt_at = time.monotonic() + min(
                _launcher.TELEMETRY_SECONDS * (2 ** (state.failures - 1)),
                _launcher.TELEMETRY_BACKOFF_MAX_SECONDS,
            )
            state.last_result = result
            if state.failures == 1:
                label = error_class or result.error or "UnknownError"
                _launcher.append_launcher_log(f"TELEMETRY REQUEST FAILED: path={path}; error={label}")

    def reset(self) -> None:
        with self._lock:
            self._paths.clear()


_TELEMETRY_REQUEST_STATE = TelemetryRequestState()


_TELEMETRY_POOL: ThreadPoolExecutor | None = None
_TELEMETRY_POOL_LOCK = threading.Lock()


def _get_telemetry_pool() -> ThreadPoolExecutor:
    global _TELEMETRY_POOL
    if _TELEMETRY_POOL is None:
        with _TELEMETRY_POOL_LOCK:
            if _TELEMETRY_POOL is None:
                _TELEMETRY_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bhm-launcher-telemetry")
    return _TELEMETRY_POOL

def fetch_telemetry(project: str | None = None) -> dict[str, str]:
    global _LAST_TELEMETRY
    project_name = _launcher.validate_launcher_project(project or _launcher.resolve_launcher_project())
    now = time.monotonic()
    encoded_project = urlencode({"project": project_name})
    requests = {
        "launcher": (f"{_launcher.BHM_BASE_URL}/bhm/telemetry/launcher", "GET", None),
        "graph": (f"{_launcher.BHM_BASE_URL}/bhm/galaxy/stats", "GET", None),
        "activity": (f"{_launcher.BHM_BASE_URL}/bhm/agent-activity-rollup", "POST", {"project": project_name}),
        "profile": (f"{_launcher.BHM_BASE_URL}/bhm/profile?{encoded_project}", "GET", None),
        "cutover": (f"{_launcher.BHM_BASE_URL}/health/cutover?{encoded_project}", "GET", None),
        "slo": (f"{_launcher.BHM_BASE_URL}/bhm/health/slo?{encoded_project}", "GET", None),
        "mcp": (f"{_launcher.BHM_BASE_URL}/bhm/telemetry/mcp-panel?{encoded_project}", "GET", None),
        "governed": (f"{_launcher.BHM_BASE_URL}/bhm/governed-consolidation/proposals?{urlencode({'project': project_name, 'status': 'proposed', 'limit': 200})}", "GET", None),
    }

    def execute(item: tuple[str, tuple[str, str, dict[str, Any] | None]]) -> tuple[str, _launcher.JsonRequestResult]:
        key, (url, method, payload) = item
        path = urlsplit(url).path
        if not _launcher._TELEMETRY_REQUEST_STATE.should_request(path, now):
            previous = _launcher._TELEMETRY_REQUEST_STATE.last_result(path)
            if previous is not None:
                return key, previous
        result = _launcher.safe_json_request(url, method=method, payload=payload, project=project_name)
        if not _launcher._TELEMETRY_REQUEST_STATE.was_recorded(path, result):
            _launcher._TELEMETRY_REQUEST_STATE.record_result(path, result)
        return key, result

    pool = _get_telemetry_pool()
    results = dict(pool.map(execute, requests.items()))

    launcher = results["launcher"].data
    graph = results["graph"].data
    activity = results["activity"].data
    profile = results["profile"].data
    cutover = results["cutover"].data
    slo = results["slo"].data
    mcp = results["mcp"].data
    governed = results["governed"].data
    counts = activity.get("counts") if isinstance(activity.get("counts"), dict) else {}
    warmup = ((profile.get("readiness") or {}).get("provider_warmup") or {}) if isinstance(profile, dict) else {}
    memory_store = cutover.get("memory_store") if isinstance(cutover.get("memory_store"), dict) else {}
    storage = cutover.get("storage") if isinstance(cutover.get("storage"), dict) else {}
    observed = slo.get("observed") if isinstance(slo.get("observed"), dict) else {}
    connected = mcp.get("connected") if isinstance(mcp.get("connected"), dict) else {}
    mcp_overall = mcp.get("overall") if isinstance(mcp.get("overall"), dict) else {}
    mcp_rest = mcp.get("rest_degraded") if isinstance(mcp.get("rest_degraded"), dict) else {}
    error_label = _launcher._result_error_label(list(results.values()))

    def value_or_error(result_key: str, value: Any) -> str:
        return str(value) if results[result_key].ok and value not in {None, ""} else results[result_key].error or error_label

    provider_state = "READY" if warmup.get("ready") else str(warmup.get("phase") or results["profile"].error or error_label).upper()
    sqlite_state = "READY" if memory_store.get("ready") else str(memory_store.get("readiness") or results["cutover"].error or error_label).upper()
    qdrant_state = "READY" if storage.get("ready") else str(storage.get("readiness") or results["cutover"].error or error_label).upper()
    attached_count = int(connected.get("attached_count") or 0)
    if attached_count:
        mcp_state = "ATTACHED"
    elif mcp_rest.get("transport_ready") is True:
        # An idle Streamable HTTP endpoint is ready for a client but cannot
        # prove a native session for this launcher. Keep it distinct from a
        # real transport outage and from the independent BHM SLO state.
        mcp_state = "IDLE · ENDPOINT READY"
    else:
        mcp_state = str(mcp_overall.get("state") or results["mcp"].error or error_label).upper()
    if attached_count:
        mcp_state = f"{mcp_state} · {attached_count}"
    projection_pending = int(observed.get("projection_pending") or 0)
    projection_failed = int(observed.get("projection_failed") or 0)
    current = {
        "memory_count": value_or_error("launcher", launcher.get("memory_count")),
        "link_count": value_or_error("graph", graph.get("link_count")),
        "node_count": value_or_error("graph", graph.get("node_count")),
        "sessions": value_or_error("launcher", launcher.get("session_count")),
        "observations": value_or_error("activity", counts.get("observations")),
        "sqlite_state": sqlite_state,
        "qdrant_state": qdrant_state,
        "provider_state": provider_state,
        "mcp_state": mcp_state,
        "projection_queue": f"{projection_pending} / {projection_failed}" if results["slo"].ok else results["slo"].error or error_label,
        "consolidation_queue": value_or_error("governed", governed.get("count")),
        "slo_state": str(slo.get("status") or results["slo"].error or error_label).upper(),
        "last_sys": datetime.now().strftime("%H:%M:%S") if any(result.ok for result in results.values()) else error_label,
    }
    _LAST_TELEMETRY = current
    return current


def tcp_status(port: int, timeout: float = LAUNCHER_TCP_PROBE_TIMEOUT_SECONDS, host: str | None = None) -> _launcher.ServiceStatus:
    if not 1 <= port <= 65535:
        return _launcher.ServiceStatus("Error", "Invalid port")
    try:
        target_host = host or endpoint_parts("llm_default")[0]
        with socket.create_connection((target_host, port), timeout=timeout):
            return _launcher.ServiceStatus("Running", f"{target_host}:{port}")
    except OSError as exc:
        return _launcher.ServiceStatus("Stopped", _launcher.compact_error(exc))


def llm_api_status(port: int, timeout: float = LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS) -> _launcher.ServiceStatus:
    if not 1 <= port <= 65535:
        return _launcher.ServiceStatus("Error", "Invalid port")
    host = endpoint_parts("llm_default")[0]
    url = f"http://{host}:{port}/v1/models"
    request = urllib.request.Request(url, headers={"User-Agent": "BHM-Control-Deck"}, method="GET")
    try:
        with _launcher.open_local_url(request, timeout=timeout) as response:
            raw = read_bounded_response(response, limit=MAX_HTTP_BYTES)
        payload = json.loads(raw.decode("utf-8"))
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return _launcher.ServiceStatus("Error", "Models response is invalid")
        return _launcher.ServiceStatus("Running", f"API ready · {len(models)} model(s)")
    except urllib.error.HTTPError as exc:
        return _launcher.ServiceStatus("Error", f"HTTP {exc.code}")
    except (LocalEndpointError, OSError, ValueError, UnicodeDecodeError):
        return _launcher.ServiceStatus("Stopped", f"{host}:{port} API unavailable")


def merge_llm_readiness_and_inventory(
    readiness: _launcher.ServiceStatus,
    inventory: _launcher.ServiceStatus | None,
) -> _launcher.ServiceStatus:
    if readiness.state != "Running" or inventory is None:
        return readiness
    if inventory.state == "Running":
        return _launcher.ServiceStatus("Running", inventory.detail)
    return _launcher.ServiceStatus("Running", f"{readiness.detail} - model inventory unavailable")


def observe_local_llm(port: int, now: float, inventory: LlmInventoryCache) -> _launcher.ServiceStatus:
    readiness = _launcher.tcp_status(port)
    if readiness.state != "Running":
        return readiness
    if now >= inventory.next_refresh_at:
        inventory.status = _launcher.llm_api_status(port)
        inventory.next_refresh_at = now + _launcher.LLM_INVENTORY_REFRESH_SECONDS
    return _launcher.merge_llm_readiness_and_inventory(readiness, inventory.status)


def remote_status(url: str) -> _launcher.ServiceStatus:
    target = url.strip()
    if not target:
        return _launcher.ServiceStatus("Error", "Remote URL is empty")
    if not re.match(r"^https?://", target, re.IGNORECASE):
        target = f"http://{target}"
    return _launcher.http_status(target, timeout=LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS)
