"""Runtime endpoint resolver for standalone BHM scripts and the launcher."""

from __future__ import annotations

import json
import ipaddress
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


_LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})


def _endpoint_netloc(host: str, port: int) -> str:
    """Render an RFC 3986 authority for a configured host and port."""

    normalized = str(host or "").strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return f"{normalized}:{port}"
    if address.version == 6:
        return f"[{normalized}]:{port}"
    return f"{normalized}:{port}"


def validate_loopback_endpoint(value: str) -> str:
    """Validate an HTTP(S) endpoint whose credentials must stay on loopback."""

    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("local-only endpoint must use http or https")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("local-only loopback endpoint must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("local-only loopback endpoint must not contain query or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("local-only loopback endpoint has an invalid port") from exc
    host = parsed.hostname.casefold().rstrip(".")
    if host in _LOOPBACK_HOSTNAMES:
        return raw
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("local-only endpoint host must be loopback-only") from exc
    if not address.is_loopback:
        raise ValueError("local-only endpoint host must be loopback-only")
    _ = port
    return raw


def _roots() -> list[Path]:
    configured = os.getenv("BHM_RUNTIME_ENDPOINTS_FILE", "").strip()
    roots = [Path(configured)] if configured else []
    here = Path(__file__).resolve()
    roots.extend([here.parents[1] / "config" / "runtime-endpoints.json", Path.cwd() / "config" / "runtime-endpoints.json"])
    if getattr(sys, "_MEIPASS", None):
        roots.insert(0, Path(sys._MEIPASS) / "config" / "runtime-endpoints.json")
    return list(dict.fromkeys(roots))


def _catalog() -> dict:
    for path in _roots():
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError("runtime-endpoints.json was not found")


def _service(name: str) -> dict:
    service = _catalog().get("services", {}).get(name)
    if not isinstance(service, dict):
        raise KeyError(f"unknown endpoint service: {name}")
    return service


def endpoint_url(name: str, path: str = "") -> str:
    service = _service(name)
    explicit = os.getenv(str(service.get("url_env") or ""), "").strip()
    if explicit:
        base = explicit.rstrip("/")
    else:
        host = os.getenv(str(service.get("host_env") or ""), str(service["host"]))
        port = int(os.getenv(str(service.get("port_env") or ""), str(service["port"])))
        base_path = str(service.get("base_path") or "")
        base = urlunsplit((str(service.get("scheme") or "http"), _endpoint_netloc(host, port), base_path, "", "")).rstrip("/")
    return f"{base}/{path.lstrip('/')}" if path else base


def endpoint_parts(name: str) -> tuple[str, int]:
    parsed = urlsplit(endpoint_url(name))
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"endpoint {name} has no host/port")
    return parsed.hostname, parsed.port


def endpoint_port(name: str) -> int:
    return endpoint_parts(name)[1]


__all__ = ["endpoint_parts", "endpoint_port", "endpoint_url", "validate_loopback_endpoint"]
