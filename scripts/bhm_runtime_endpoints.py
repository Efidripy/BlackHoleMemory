"""Runtime endpoint resolver for standalone BHM scripts and the launcher."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


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
        base = urlunsplit((str(service.get("scheme") or "http"), f"{host}:{port}", base_path, "", "")).rstrip("/")
    return f"{base}/{path.lstrip('/')}" if path else base


def endpoint_parts(name: str) -> tuple[str, int]:
    parsed = urlsplit(endpoint_url(name))
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"endpoint {name} has no host/port")
    return parsed.hostname, parsed.port


def endpoint_port(name: str) -> int:
    return endpoint_parts(name)[1]


__all__ = ["endpoint_parts", "endpoint_port", "endpoint_url"]
