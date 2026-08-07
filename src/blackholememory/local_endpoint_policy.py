"""Fail-closed transport policy for local-only HTTP providers."""

from __future__ import annotations

import ipaddress
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit


MAX_RESPONSE_BYTES = 256 * 1024
_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class LocalEndpointError(urllib.error.URLError):
    """The provider URL violates the local-only transport contract."""


def _is_local_host(host: str) -> bool:
    normalized = host.casefold().rstrip(".")
    if normalized in _LOCAL_HOSTNAMES or normalized.endswith(".test"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    if address.is_unspecified or address.is_multicast:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if address.version == 4:
        return bool(
            address.is_loopback
            or address.is_link_local
            or address in ipaddress.ip_network("10.0.0.0/8")
            or address in ipaddress.ip_network("172.16.0.0/12")
            or address in ipaddress.ip_network("192.168.0.0/16")
        )
    return bool(address.is_loopback or address.is_link_local or address in ipaddress.ip_network("fc00::/7"))


def validate_local_endpoint(value: str) -> str:
    """Validate and normalize an HTTP(S) endpoint that must stay local."""

    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() not in _ALLOWED_SCHEMES:
        raise LocalEndpointError("local-only provider requires http or https")
    if not parsed.hostname or parsed.username or parsed.password:
        raise LocalEndpointError("local-only provider URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise LocalEndpointError("local-only provider URL must not contain query or fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise LocalEndpointError("local-only provider URL has an invalid port") from exc
    if not _is_local_host(parsed.hostname):
        raise LocalEndpointError("local-only provider endpoint is not loopback/private")
    return raw.rstrip("/")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def _reject(self, *_args: Any, **_kwargs: Any) -> Any:
        raise LocalEndpointError("redirects are disabled for local-only provider")

    http_error_301 = _reject
    http_error_302 = _reject
    http_error_303 = _reject
    http_error_307 = _reject
    http_error_308 = _reject


def open_local_url(
    request: urllib.request.Request,
    *,
    timeout: float,
    endpoint: str | None = None,
):
    """Open a local request with proxies and redirects disabled.

    ``endpoint`` is the validated local origin when the request carries query
    parameters. The endpoint policy intentionally rejects queries on the
    configured base URL, while ordinary request query strings remain valid.
    The request must still target the same scheme/host/port as that origin.
    """

    configured = validate_local_endpoint(endpoint or request.full_url)
    if endpoint is not None:
        origin = urlsplit(configured)
        target = urlsplit(request.full_url)
        if target.username or target.password or target.fragment:
            raise LocalEndpointError("local-only request target must not contain credentials or fragment")
        if (
            target.scheme.casefold() != origin.scheme.casefold()
            or (target.hostname or "").casefold().rstrip(".")
            != (origin.hostname or "").casefold().rstrip(".")
            or target.port != origin.port
        ):
            raise LocalEndpointError("local-only request target differs from configured endpoint")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )
    return opener.open(request, timeout=timeout)


def read_bounded_response(response: Any, *, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """Read at most ``limit`` bytes and fail closed on oversized responses."""

    bounded_limit = max(int(limit), 1)
    payload = response.read(bounded_limit + 1)
    if len(payload) > bounded_limit:
        raise LocalEndpointError("local-only provider response exceeded the bounded limit")
    return payload


__all__ = [
    "MAX_RESPONSE_BYTES",
    "LocalEndpointError",
    "open_local_url",
    "read_bounded_response",
    "validate_local_endpoint",
]
