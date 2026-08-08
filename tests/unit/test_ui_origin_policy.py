from __future__ import annotations

import pytest

from blackholememory.ui_origin_policy import browser_request_is_same_origin
from blackholememory.ui_origin_policy import request_host_parts
from blackholememory.ui_origin_policy import request_is_loopback
from blackholememory.ui_origin_policy import websocket_origin_is_allowed


PORT = 8000


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("127.0.0.1:8000", ("127.0.0.1", "127.0.0.1:8000")),
        ("LOCALHOST:8000", ("localhost", "localhost:8000")),
        ("[::1]:8000", ("::1", "[::1]:8000")),
        ("127.0.0.1", None),
        ("127.0.0.1:8001", None),
        ("user:pass@127.0.0.1:8000", None),
        ("192.0.2.10:8000", None),
        ("not a host", None),
    ],
)
def test_request_host_parts_is_loopback_and_port_bound(value: str, expected: tuple[str, str] | None) -> None:
    assert request_host_parts(value, canonical_port=PORT) == expected


@pytest.mark.parametrize("value", ["127.0.0.1", "::1", "localhost", "LOCALHOST"])
def test_request_is_loopback_accepts_local_clients(value: str) -> None:
    assert request_is_loopback(value) is True


@pytest.mark.parametrize("value", ["", "192.0.2.10", "localhost.localdomain", "not-an-ip"])
def test_request_is_loopback_rejects_remote_or_unknown_clients(value: str) -> None:
    assert request_is_loopback(value) is False


def test_browser_same_origin_requires_fetch_site_and_matches_origin() -> None:
    kwargs = {
        "host_header": "127.0.0.1:8000",
        "sec_fetch_site": "same-origin",
        "origin_header": "http://127.0.0.1:8000",
        "request_scheme": "http",
        "canonical_port": PORT,
    }
    assert browser_request_is_same_origin(**kwargs) is True
    assert browser_request_is_same_origin(**{**kwargs, "origin_header": "http://127.0.0.1:8001"}) is False
    assert browser_request_is_same_origin(**{**kwargs, "sec_fetch_site": "cross-site"}) is False
    assert browser_request_is_same_origin(**{**kwargs, "origin_header": "https://127.0.0.1:8000"}) is False


def test_browser_same_origin_origin_header_requirement_is_explicit() -> None:
    kwargs = {
        "host_header": "localhost:8000",
        "sec_fetch_site": "same-origin",
        "origin_header": "",
        "request_scheme": "http",
        "canonical_port": PORT,
    }
    assert browser_request_is_same_origin(**kwargs) is True
    assert browser_request_is_same_origin(**kwargs, require_origin=True) is False
    assert browser_request_is_same_origin(**{**kwargs, "origin_header": "not an origin"}) is False


def test_websocket_origin_policy_distinguishes_bearer_and_ui_session() -> None:
    base = {
        "host_header": "[::1]:8000",
        "origin_header": "http://[::1]:8000",
        "canonical_port": PORT,
    }
    assert websocket_origin_is_allowed(**base, require_exact_origin=True) is True
    assert websocket_origin_is_allowed(**{**base, "origin_header": "http://[::1]:8001"}, require_exact_origin=True) is False
    assert websocket_origin_is_allowed(**{**base, "origin_header": ""}, require_exact_origin=False) is True
    assert websocket_origin_is_allowed(**{**base, "origin_header": ""}, require_exact_origin=True) is False
    assert websocket_origin_is_allowed(**{**base, "origin_header": "http://192.0.2.10:8000"}, require_exact_origin=False) is False
