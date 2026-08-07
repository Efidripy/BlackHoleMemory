"""Fail-closed policy for operator-configured public HTTPS APIs."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


class PublicEndpointError(ValueError):
    """Raised when an external API endpoint violates the public HTTPS contract."""


def validate_public_https_endpoint(value: str) -> str:
    """Validate a public HTTPS endpoint without credentials or fragments."""

    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() != "https":
        raise PublicEndpointError("external API endpoint must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise PublicEndpointError("external API endpoint must have a host without credentials or fragment")
    normalized_host = parsed.hostname.casefold().rstrip(".")
    if normalized_host in _LOCAL_HOSTNAMES or normalized_host.endswith(_LOCAL_SUFFIXES):
        raise PublicEndpointError("external API endpoint must not use a local hostname")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or not address.is_global
    ):
        raise PublicEndpointError("external API endpoint must not target a private or local address")
    return raw.rstrip("/")


__all__ = ["PublicEndpointError", "validate_public_https_endpoint"]
