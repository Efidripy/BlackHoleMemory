from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

from blackholememory.local_endpoint_policy import LocalEndpointError


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bhm_night_watch.py"
SPEC = importlib.util.spec_from_file_location("bhm_night_watch_transport", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class _Response:
    status = 200

    def __init__(self, body: bytes):
        self.body = body
        self.limits: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        self.limits.append(limit)
        return self.body if len(self.body) <= limit else self.body[:limit]


def test_post_json_uses_local_bounded_transport_and_auth(monkeypatch):
    response = _Response(json.dumps({"results": []}).encode())
    captured: dict[str, object] = {}

    def fake_open(request, *, timeout, endpoint):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["endpoint"] = endpoint
        captured["headers"] = dict(request.headers)
        return response

    monkeypatch.setenv("BHM_CALLER_TOKEN", "t" * 32)
    monkeypatch.setattr(MODULE, "open_local_url", fake_open)

    result = MODULE.post_json("http://127.0.0.1:8000", "/bhm/search", {"query": "TODO"}, 3)

    assert result == {"results": []}
    assert captured["url"] == "http://127.0.0.1:8000/bhm/search"
    assert captured["endpoint"] == "http://127.0.0.1:8000"
    assert captured["timeout"] == 3
    assert captured["headers"]["Authorization"] == f"Bearer {'t' * 32}"
    assert response.limits == [MODULE.MAX_HTTP_RESPONSE_BYTES + 1]


def test_night_watch_timeout_is_registry_bounded() -> None:
    assert MODULE.DEFAULT_TIMEOUT_SECONDS == 15.0
    assert MODULE.bounded_night_watch_timeout(3) == 3.0
    assert MODULE.bounded_night_watch_timeout(999) == 15.0
    assert MODULE.bounded_night_watch_timeout(0) == 1.0
    with pytest.raises(ValueError, match="finite"):
        MODULE.bounded_night_watch_timeout(float("inf"))


def test_post_json_rejects_external_base_before_open(monkeypatch):
    called = False

    def fail_open(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("external endpoint must not be opened")

    monkeypatch.setattr(MODULE, "open_local_url", fail_open)
    with pytest.raises(RuntimeError, match="endpoint rejected"):
        MODULE.post_json("https://example.com", "/bhm/search", {}, 1)
    assert called is False


def test_post_json_fails_closed_on_oversized_response(monkeypatch):
    response = _Response(b"x" * (MODULE.MAX_HTTP_RESPONSE_BYTES + 1))
    monkeypatch.setattr(MODULE, "open_local_url", lambda *_args, **_kwargs: response)
    with pytest.raises(RuntimeError, match="bounded"):
        MODULE.post_json("http://127.0.0.1:8000", "/bhm/search", {}, 1)


def test_open_local_url_rejects_night_watch_origin_mismatch():
    req = MODULE.urllib.request.Request("http://127.0.0.2:8000/bhm/search")
    with pytest.raises(LocalEndpointError, match="differs"):
        MODULE.open_local_url(req, timeout=1, endpoint="http://127.0.0.1:8000")
