"""Single source of truth for local BHM endpoint defaults.

The checked-in JSON contains safe loopback defaults. Every host/port can be
overridden through the service-specific environment variables declared there;
an explicit URL variable wins over host/port composition.
"""

from __future__ import annotations

import json
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class EndpointConfigError(RuntimeError):
    """Raised when the endpoint catalog is missing or malformed."""


_LOCAL_LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})


def validate_loopback_listener_host(host: str) -> str:
    """Enforce the local-only listener contract for the BHM API.

    The API carries browser session bootstrap and local caller authority, so a
    wildcard or LAN listener must fail closed before Uvicorn starts. DNS names
    other than the two explicit localhost aliases are rejected because their
    eventual address cannot be proven from configuration alone.
    """

    normalized = str(host or "").strip().casefold().rstrip(".")
    if normalized in _LOCAL_LOOPBACK_HOSTNAMES:
        return normalized
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise EndpointConfigError(
            "BHM API listener host must be loopback-only (localhost, 127.0.0.1, or ::1)"
        ) from exc
    if not address.is_loopback:
        raise EndpointConfigError(
            "BHM API listener host must be loopback-only (localhost, 127.0.0.1, or ::1)"
        )
    return normalized


@dataclass(frozen=True)
class Endpoint:
    name: str
    scheme: str
    host: str
    port: int
    base_path: str = ""
    url_env: str = ""

    @property
    def url(self) -> str:
        path = self.base_path or ""
        return urlunsplit((self.scheme, f"{self.host}:{self.port}", path, "", "")).rstrip("/")


def _catalog_candidates() -> list[Path]:
    configured = os.getenv("BHM_RUNTIME_ENDPOINTS_FILE", "").strip()
    candidates = [Path(configured)] if configured else []
    module_root = Path(__file__).resolve().parents[2]
    candidates.extend((module_root / "config" / "runtime-endpoints.json", Path.cwd() / "config" / "runtime-endpoints.json"))
    return list(dict.fromkeys(candidates))


def _load_catalog() -> dict[str, Any]:
    for path in _catalog_candidates():
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EndpointConfigError(f"unable to read endpoint catalog {path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise EndpointConfigError(f"unsupported endpoint catalog: {path}")
        services = payload.get("services")
        if not isinstance(services, dict) or not services:
            raise EndpointConfigError(f"endpoint catalog has no services: {path}")
        return payload
    raise EndpointConfigError("runtime-endpoints.json was not found")


def _int_env(name: str, default: Any) -> int:
    raw = os.getenv(name, "").strip() if name else ""
    value = int(raw) if raw else int(default)
    if not 1 <= value <= 65535:
        raise EndpointConfigError(f"{name or 'endpoint port'} must be between 1 and 65535")
    return value


def endpoint(name: str) -> Endpoint:
    service = _load_catalog().get("services", {}).get(name)
    if not isinstance(service, dict):
        raise EndpointConfigError(f"unknown endpoint service: {name}")
    host_env = str(service.get("host_env") or "")
    port_env = str(service.get("port_env") or "")
    host = os.getenv(host_env, str(service.get("host") or "127.0.0.1")).strip() or str(service.get("host"))
    port = _int_env(port_env, service.get("port"))
    scheme = str(service.get("scheme") or "http")
    base_path = str(service.get("base_path") or "")
    return Endpoint(name=name, scheme=scheme, host=host, port=port, base_path=base_path, url_env=str(service.get("url_env") or ""))


def endpoint_url(name: str, path: str = "") -> str:
    item = endpoint(name)
    explicit = os.getenv(item.url_env, "").strip() if item.url_env else ""
    base = explicit.rstrip("/") if explicit else item.url
    if not path:
        return base
    return f"{base}/{path.lstrip('/')}"


def endpoint_port(name: str) -> int:
    return endpoint(name).port


def endpoint_host(name: str) -> str:
    return endpoint(name).host


def endpoint_parts(name: str) -> tuple[str, int]:
    item = endpoint(name)
    explicit = os.getenv(item.url_env, "").strip() if item.url_env else ""
    if explicit:
        parsed = urlsplit(explicit)
        if parsed.hostname and parsed.port:
            return parsed.hostname, parsed.port
    return item.host, item.port


__all__ = [
    "Endpoint",
    "EndpointConfigError",
    "endpoint",
    "endpoint_host",
    "endpoint_parts",
    "endpoint_port",
    "endpoint_url",
    "validate_loopback_listener_host",
]
