"""Bounded, local-only IPv4/IPv6 readiness probe for the configured LLM.

The probe is intentionally observational.  It requests only ``/v1/models``
through the existing no-proxy/no-redirect local endpoint policy and never
starts a model, changes a server bind, or writes BHM state.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from .local_endpoint_policy import LocalEndpointError
from .local_endpoint_policy import open_local_url
from .local_endpoint_policy import read_bounded_response


SCHEMA_VERSION = "bhm.local-llm-dualstack.v1"
DEFAULT_TIMEOUT_SECONDS = 2.0
_LOOPBACKS = (("ipv4", "127.0.0.1"), ("ipv6", "::1"))


def _port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def _timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be numeric") from exc
    if not 0.05 <= timeout <= 10.0:
        raise ValueError("timeout must be between 0.05 and 10 seconds")
    return timeout


def endpoint_url(host: str, port: int) -> str:
    """Return the exact local OpenAI-compatible models URL for one family."""

    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("dual-stack probe host must be an exact loopback literal")
    authority = f"[{host}]" if host == "::1" else host
    return f"http://{authority}:{_port(port)}/v1/models"


def _failure_kind(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(error, urllib.error.HTTPError):
        return "http_error"
    if isinstance(error, (LocalEndpointError, urllib.error.URLError)):
        reason = getattr(error, "reason", None)
        if isinstance(reason, ConnectionRefusedError):
            return "connection_refused"
        if isinstance(reason, TimeoutError):
            return "timeout"
        return "transport_error"
    if isinstance(error, (UnicodeDecodeError, json.JSONDecodeError, ValueError)):
        return "invalid_response"
    return "transport_error"


def probe_family(host: str, port: int, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Probe a literal loopback family without leaking provider payloads."""

    timeout = _timeout(timeout_seconds)
    url = endpoint_url(host, port)
    started = time.perf_counter()
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with open_local_url(request, timeout=timeout, endpoint=url) as response:
            payload = json.loads(read_bounded_response(response).decode("utf-8"))
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
            raise ValueError("models response has no data array")
        return {
            "host": host,
            "status": "ready",
            "http_status": 200,
            "model_count": len(payload["data"]),
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    except Exception as exc:  # response details can expose provider/runtime internals
        return {
            "host": host,
            "status": _failure_kind(exc),
            "http_status": None,
            "model_count": 0,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }


def dualstack_report(port: int, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Return a content-limited readiness classification for both loopbacks."""

    normalized_port = _port(port)
    probes = {
        family: probe_family(host, normalized_port, timeout_seconds=timeout_seconds)
        for family, host in _LOOPBACKS
    }
    ipv4_ready = probes["ipv4"]["status"] == "ready"
    ipv6_ready = probes["ipv6"]["status"] == "ready"
    readiness = (
        "dual_stack_ready"
        if ipv4_ready and ipv6_ready
        else "ipv4_only"
        if ipv4_ready
        else "ipv6_only"
        if ipv6_ready
        else "unavailable"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": ipv4_ready,
        "readiness": readiness,
        "port": normalized_port,
        "probes": probes,
        "execution": {
            "read_only": True,
            "model_started": False,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "proxies_disabled": True,
            "redirects_disabled": True,
        },
    }


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "SCHEMA_VERSION",
    "dualstack_report",
    "endpoint_url",
    "probe_family",
]
