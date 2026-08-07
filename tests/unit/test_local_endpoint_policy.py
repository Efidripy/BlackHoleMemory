from __future__ import annotations

import urllib.request

import pytest

from blackholememory.local_endpoint_policy import LocalEndpointError
from blackholememory.local_endpoint_policy import open_local_url
from blackholememory.local_endpoint_policy import read_bounded_response
from blackholememory.local_endpoint_policy import validate_local_endpoint
from blackholememory.local_endpoint_policy import _NoRedirectHandler


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:13666/v1",
        "https://localhost:8443/v1",
        "http://[::1]:13666/v1",
        "http://10.0.0.8:9000/v1",
        "http://[fd00::8]:9000/v1",
        "http://provider.test:9000/v1",
        "http://[::ffff:10.0.0.8]:9000/v1",
    ],
)
def test_validate_local_endpoint_accepts_loopback_private_and_test_hosts(url: str) -> None:
    assert validate_local_endpoint(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/v1",
        "ftp://127.0.0.1:13666/v1",
        "http://user:secret@127.0.0.1:13666/v1",
        "http://127.0.0.1:13666/v1?redirect=https://example.com",
        "http://127.0.0.1:bad/v1",
        "http://0.0.0.0:9000/v1",
        "http://[::]:9000/v1",
        "http://224.0.0.1:9000/v1",
        "http://[ff02::1]:9000/v1",
    ],
)
def test_validate_local_endpoint_rejects_external_or_ambiguous_urls(url: str) -> None:
    with pytest.raises(LocalEndpointError):
        validate_local_endpoint(url)


def test_read_bounded_response_rejects_oversized_payload() -> None:
    class Response:
        def read(self, limit: int) -> bytes:
            return b"x" * limit

    with pytest.raises(LocalEndpointError, match="bounded limit"):
        read_bounded_response(Response(), limit=8)


def test_open_local_url_disables_environment_proxies(monkeypatch) -> None:
    captured = {}

    class Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return object()

    def build_opener(*handlers):
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    request = urllib.request.Request("http://127.0.0.1:13666/v1/chat/completions")
    assert open_local_url(request, timeout=2.0) is not None
    assert any(isinstance(handler, urllib.request.ProxyHandler) and handler.proxies == {} for handler in captured["handlers"])
    assert any(handler.__class__.__name__ == "_NoRedirectHandler" for handler in captured["handlers"])


@pytest.mark.parametrize(
    "url",
    [
        "http://user:secret@127.0.0.1:13666/v1",
        "http://127.0.0.1:13666/v1#fragment",
    ],
)
def test_open_local_url_rejects_target_credentials_and_fragments(url: str, monkeypatch) -> None:
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: pytest.fail("network open must not run"))
    with pytest.raises(LocalEndpointError, match="credentials or fragment"):
        open_local_url(
            urllib.request.Request(url),
            timeout=2.0,
            endpoint="http://127.0.0.1:13666",
        )


def test_no_redirect_handler_rejects_redirect_response() -> None:
    handler = _NoRedirectHandler()
    request = urllib.request.Request("http://127.0.0.1:13666/v1")
    with pytest.raises(LocalEndpointError, match="redirects are disabled"):
        handler.http_error_302(request, object(), 302, "Found", {})
