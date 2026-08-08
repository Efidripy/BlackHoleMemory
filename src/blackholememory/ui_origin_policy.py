"""Pure origin and loopback predicates for the browser UI auth boundary.

The helpers intentionally accept parsed header values instead of framework
objects.  This keeps the security policy unit-testable while allowing the
application to resolve its configured listener port at request time.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def request_host_parts(host_header: str | None, *, canonical_port: int) -> tuple[str, str] | None:
    raw_host = str(host_header or "").strip()
    if not raw_host:
        return None
    try:
        parsed = urlsplit(f"http://{raw_host}")
        hostname = str(parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return None
    if hostname not in LOOPBACK_HOSTS or port != canonical_port or parsed.username or parsed.password:
        return None
    return hostname, raw_host.casefold()


def browser_request_is_same_origin(
    *,
    host_header: str | None,
    sec_fetch_site: str | None,
    origin_header: str | None,
    request_scheme: str,
    canonical_port: int,
    require_origin: bool = False,
) -> bool:
    host_parts = request_host_parts(host_header, canonical_port=canonical_port)
    if host_parts is None:
        return False
    if str(sec_fetch_site or "").strip().casefold() != "same-origin":
        return False
    origin_value = str(origin_header or "").strip()
    if not origin_value:
        return not require_origin
    try:
        origin = urlsplit(origin_value)
        origin_host = str(origin.hostname or "").casefold()
    except ValueError:
        return False
    return (
        origin.scheme.casefold() == str(request_scheme).casefold()
        and origin_host in LOOPBACK_HOSTS
        and origin.netloc.casefold() == host_parts[1]
    )


def request_is_loopback(client_host: str | None) -> bool:
    host = str(client_host or "").strip()
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.casefold() == "localhost"


def websocket_origin_is_allowed(
    *,
    host_header: str | None,
    origin_header: str | None,
    canonical_port: int,
    require_exact_origin: bool,
) -> bool:
    host_parts = request_host_parts(host_header, canonical_port=canonical_port)
    if host_parts is None:
        return False
    origin_value = str(origin_header or "").strip()
    if not origin_value:
        return not require_exact_origin
    try:
        origin = urlsplit(origin_value)
        origin_host = str(origin.hostname or "").casefold()
    except ValueError:
        return False
    if origin.scheme.casefold() not in {"http", "https"} or origin_host not in LOOPBACK_HOSTS:
        return False
    return not require_exact_origin or origin.netloc.casefold() == host_parts[1]


__all__ = [
    "LOOPBACK_HOSTS",
    "browser_request_is_same_origin",
    "request_host_parts",
    "request_is_loopback",
    "websocket_origin_is_allowed",
]
