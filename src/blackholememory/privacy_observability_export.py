"""Default-off, content-free observability export contracts.

The functions here render only already-aggregated counters into a bounded
Prometheus-compatible payload and prepare a *plan* for a future sender. They do
not open sockets, resolve DNS, modify configuration or persist any data. This
keeps observability outside the SQLite authority and prevents accidental egress.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


SCHEMA_VERSION = "bhm.privacy-observability-export.v1"
MAX_INPUT_ROWS = 64
MAX_RENDERED_SERIES = 128
_ROUTES = {
    "GET_/bhm/health": "health",
    "GET_/bhm/projects": "projects",
    "POST_/bhm/search": "search",
    "tools/call:bhm_health": "mcp_health",
    "tools/call:bhm_search": "mcp_search",
}
_STATUS = {"2xx", "4xx", "5xx", "timeout", "other"}
_OUTBOX = {"pending", "claimed", "deferred", "failed", "completed", "dead_letter", "other"}
_ROUTE_LABEL = "(?:health|projects|search|mcp_health|mcp_search|other)"
_SURFACE_LABEL = "(?:rest|mcp|other)"
_STATUS_LABEL = "(?:2xx|4xx|5xx|timeout|other)"
_OUTBOX_LABEL = "(?:pending|claimed|deferred|failed|completed|dead_letter|other)"
_NUMBER = r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,3})?"
_SAFE_METRIC_LINE = re.compile(
    "(?:"
    r"# TYPE bhm_observability_(?:requests_total counter|request_latency_ms gauge|outbox_events_total gauge)"
    rf"|bhm_observability_requests_total\{{route=\"{_ROUTE_LABEL}\",status=\"{_STATUS_LABEL}\",surface=\"{_SURFACE_LABEL}\"\}} {_NUMBER}"
    rf"|bhm_observability_request_latency_ms\{{quantile=\"(?:0\.50|0\.95)\",route=\"{_ROUTE_LABEL}\",surface=\"{_SURFACE_LABEL}\"\}} {_NUMBER}"
    rf"|bhm_observability_outbox_events_total\{{state=\"{_OUTBOX_LABEL}\"\}} {_NUMBER}"
    r")$"
)
_PRIVACY = {"content": False, "identifiers": False, "query": False, "path": False, "errors": False}
_EXECUTION = {"network_performed": False, "persistent_mutation": False}


class ObservabilityExportError(ValueError):
    """Raised when a payload or an egress plan violates the privacy contract."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _finite_nonnegative(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ObservabilityExportError(f"{field_name} must be finite") from error
    if not math.isfinite(number) or number < 0:
        raise ObservabilityExportError(f"{field_name} must be finite and non-negative")
    return round(number, 3)


def _count(value: Any, field_name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ObservabilityExportError(f"{field_name} must be a non-negative integer") from error
    if number < 0 or str(number) != str(value).strip():
        raise ObservabilityExportError(f"{field_name} must be a non-negative integer")
    return number


def _route(value: Any) -> str:
    return _ROUTES.get(str(value or ""), "other")


def _surface(value: Any) -> str:
    return str(value or "").lower() if str(value or "").lower() in {"rest", "mcp"} else "other"


def _status(value: Any) -> str:
    return str(value or "").lower() if str(value or "").lower() in _STATUS else "other"


def _metric(name: str, labels: Mapping[str, str], value: int | float) -> str:
    rendered_labels = ",".join(f'{key}="{labels[key]}"' for key in sorted(labels))
    return f"{name}{{{rendered_labels}}} {value}"


@dataclass(frozen=True)
class ObservabilityExportConfig:
    """Operator-supplied configuration; disabled is the only default."""

    enabled: bool = False
    endpoint: str | None = None
    allowed_hosts: tuple[str, ...] = ()
    allow_redirects: bool = False

    def validated_endpoint(self) -> str:
        if not self.enabled:
            raise ObservabilityExportError("observability export is disabled")
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise ObservabilityExportError("enabled export requires an endpoint")
        parsed = urlsplit(self.endpoint.strip())
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ObservabilityExportError("export endpoint must be a credential-free HTTPS URL")
        try:
            port = parsed.port
        except ValueError as error:
            raise ObservabilityExportError("export endpoint has an invalid port") from error
        if parsed.query or parsed.fragment or port not in (None, 443):
            raise ObservabilityExportError("export endpoint has an unsafe component")
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            raise ObservabilityExportError("export endpoint cannot be an IP literal")
        hosts = {host.strip().lower() for host in self.allowed_hosts if isinstance(host, str) and host.strip()}
        if parsed.hostname.lower() not in hosts:
            raise ObservabilityExportError("export endpoint host is not allowlisted")
        if self.allow_redirects:
            raise ObservabilityExportError("observability export redirects are forbidden")
        return parsed.geturl()


def build_prometheus_payload(
    usage_snapshot: Mapping[str, Any],
    *,
    outbox_counts: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a bounded content-free metric receipt from local aggregate input."""

    rows = usage_snapshot.get("operations")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or len(rows) > MAX_INPUT_ROWS:
        raise ObservabilityExportError("operations must be a bounded sequence")
    request_counts: Counter[tuple[str, str, str]] = Counter()
    latency: dict[tuple[str, str], list[float]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ObservabilityExportError(f"operation row {index} is invalid")
        key = (_surface(row.get("surface")), _route(row.get("operation")), "other")
        statuses = row.get("status_counts", {})
        if not isinstance(statuses, Mapping):
            raise ObservabilityExportError(f"operation row {index} status_counts is invalid")
        for status, count in statuses.items():
            request_counts[(key[0], key[1], _status(status))] += _count(count, "status count")
        latency_value = row.get("latency_ms", {})
        if not isinstance(latency_value, Mapping):
            raise ObservabilityExportError(f"operation row {index} latency_ms is invalid")
        latency[(key[0], key[1])] = [
            _finite_nonnegative(latency_value.get("p50", 0), "latency p50"),
            _finite_nonnegative(latency_value.get("p95", 0), "latency p95"),
        ]
    rendered: list[str] = ["# TYPE bhm_observability_requests_total counter"]
    for (surface, route, status), count in sorted(request_counts.items()):
        rendered.append(_metric("bhm_observability_requests_total", {"route": route, "status": status, "surface": surface}, count))
    rendered.append("# TYPE bhm_observability_request_latency_ms gauge")
    for (surface, route), (p50, p95) in sorted(latency.items()):
        labels = {"route": route, "surface": surface}
        rendered.append(_metric("bhm_observability_request_latency_ms", {**labels, "quantile": "0.50"}, p50))
        rendered.append(_metric("bhm_observability_request_latency_ms", {**labels, "quantile": "0.95"}, p95))
    rendered.append("# TYPE bhm_observability_outbox_events_total gauge")
    normalized_outbox: Counter[str] = Counter()
    for state, count in outbox_counts.items():
        normalized_outbox[str(state).lower() if str(state).lower() in _OUTBOX else "other"] += _count(count, "outbox count")
    for state, count in sorted(normalized_outbox.items()):
        rendered.append(_metric("bhm_observability_outbox_events_total", {"state": state}, count))
    if len(rendered) > MAX_RENDERED_SERIES + 3:
        raise ObservabilityExportError("rendered metric cardinality exceeds the contract")
    core = {"schema_version": SCHEMA_VERSION, "format": "prometheus-text-v0.0.4", "metrics": rendered}
    return {
        **core,
        "payload_digest": _digest(core),
        "privacy": _PRIVACY,
        "execution": _EXECUTION,
    }


def verify_prometheus_payload(payload: Mapping[str, Any]) -> bool:
    core = {key: payload.get(key) for key in ("schema_version", "format", "metrics")}
    return (
        core["schema_version"] == SCHEMA_VERSION
        and core["format"] == "prometheus-text-v0.0.4"
        and isinstance(core["metrics"], list)
        and len(core["metrics"]) <= MAX_RENDERED_SERIES + 3
        and all(isinstance(line, str) and _SAFE_METRIC_LINE.fullmatch(line) for line in core["metrics"])
        and payload.get("privacy") == _PRIVACY
        and payload.get("execution") == _EXECUTION
        and payload.get("payload_digest") == _digest(core)
    )


def build_observability_export_plan(
    config: ObservabilityExportConfig,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a non-executing egress plan; callers cannot send from this API."""

    if not verify_prometheus_payload(payload):
        raise ObservabilityExportError("observability payload digest is invalid")
    if not config.enabled:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "disabled",
            "network_performed": False,
            "rollback": "retain-enabled-false",
        }
    endpoint = config.validated_endpoint()
    core = {
        "schema_version": SCHEMA_VERSION,
        "status": "planned",
        "endpoint": endpoint,
        "payload_digest": payload["payload_digest"],
        "method": "POST",
        "tls_required": True,
        "redirects_allowed": False,
        "network_performed": False,
    }
    return {**core, "plan_digest": _digest(core), "rollback": "set-enabled-false-and-discard-plan"}


__all__ = [
    "MAX_INPUT_ROWS",
    "ObservabilityExportConfig",
    "ObservabilityExportError",
    "SCHEMA_VERSION",
    "build_observability_export_plan",
    "build_prometheus_payload",
    "verify_prometheus_payload",
]
