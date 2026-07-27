"""Canonical local Qdrant runtime contract.

The compose file and operator wrappers are deliberately validated as text so
the gate remains usable before optional YAML dependencies are installed.
"""

from __future__ import annotations

import re
from typing import Any

from .runtime_endpoints import endpoint_host
from .runtime_endpoints import endpoint_port
from .runtime_endpoints import endpoint_url

QDRANT_IMAGE_REPOSITORY = "qdrant/qdrant"
QDRANT_IMAGE_VERSION = "v1.18.2"
QDRANT_IMAGE_DIGEST = "sha256:75eab8c4ba42096724fdcfde8b4de0b5713d529dde32f285a1f86fdcb2c9e50c"
QDRANT_IMAGE_REF = f"{QDRANT_IMAGE_REPOSITORY}:{QDRANT_IMAGE_VERSION}@{QDRANT_IMAGE_DIGEST}"

QDRANT_LOOPBACK_HOST = endpoint_host("qdrant_http")
QDRANT_HTTP_PORT = endpoint_port("qdrant_http")
QDRANT_GRPC_PORT = endpoint_port("qdrant_grpc")
QDRANT_DEFAULT_URL = endpoint_url("qdrant_http")

_IMAGE_LINE = re.compile(r"(?m)^\s+image:\s+(?P<value>[^\s#]+)\s*$")
_PORT_LINE = re.compile(r"(?m)^\s*-\s*[\"'](?P<value>[^\"']+)[\"']\s*$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _compose_image(compose_text: str) -> str | None:
    match = _IMAGE_LINE.search(compose_text)
    return match.group("value") if match else None


def _compose_port_bindings(compose_text: str) -> list[str]:
    bindings: list[str] = []
    for match in _PORT_LINE.finditer(compose_text):
        value = match.group("value")
        normalized = re.sub(r"\$\{[^}]+\}", "PORT", value)
        if normalized.count(":") == 2:
            bindings.append(value)
    return bindings


def validate_qdrant_runtime(
    compose_text: str,
    *,
    launcher_text: str = "",
) -> dict[str, Any]:
    """Return a deterministic report for the Qdrant image and host binding."""

    image = _compose_image(compose_text)
    expected_bindings = [
        f"{QDRANT_LOOPBACK_HOST}:{QDRANT_HTTP_PORT}:6333",
        f"{QDRANT_LOOPBACK_HOST}:{QDRANT_GRPC_PORT}:6334",
        "127.0.0.1:${BHM_QDRANT_HTTP_PORT:-6333}:6333",
        "127.0.0.1:${BHM_QDRANT_GRPC_PORT:-6334}:6334",
    ]
    bindings = _compose_port_bindings(compose_text)
    allowed_binding_sets = (
        {
            f"{QDRANT_LOOPBACK_HOST}:{QDRANT_HTTP_PORT}:6333",
            f"{QDRANT_LOOPBACK_HOST}:{QDRANT_GRPC_PORT}:6334",
        },
        {
            "127.0.0.1:${BHM_QDRANT_HTTP_PORT:-6333}:6333",
            "127.0.0.1:${BHM_QDRANT_GRPC_PORT:-6334}:6334",
        },
    )
    checks = {
        "image_pinned": image == QDRANT_IMAGE_REF,
        "image_has_version": bool(image and f":{QDRANT_IMAGE_VERSION}@" in image),
        "image_has_digest": bool(image and "@" in image and _SHA256.fullmatch(image.rsplit("@", 1)[1])),
        "loopback_bindings": set(bindings) in allowed_binding_sets,
        "launcher_uses_pinned_image": not launcher_text
        or (QDRANT_IMAGE_REF in launcher_text and '["docker", "pull", QDRANT_IMAGE]' in launcher_text),
        "no_latest_image": "qdrant/qdrant:latest" not in compose_text and "qdrant/qdrant:latest" not in launcher_text,
    }
    return {
        "ok": all(checks.values()),
        "image": image,
        "expected_image": QDRANT_IMAGE_REF,
        "image_version": QDRANT_IMAGE_VERSION,
        "image_digest": QDRANT_IMAGE_DIGEST,
        "bindings": bindings,
        "expected_bindings": expected_bindings,
        "checks": checks,
    }
