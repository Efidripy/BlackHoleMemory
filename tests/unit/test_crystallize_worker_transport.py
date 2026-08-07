from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from blackholememory.local_endpoint_policy import LocalEndpointError


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bhm_crystallize_worker.py"
SPEC = importlib.util.spec_from_file_location("bhm_crystallize_worker_transport", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
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


def test_rest_call_uses_validated_local_transport_and_preserves_query_and_auth(monkeypatch):
    response = _Response(json.dumps({"ok": True}).encode())
    captured: dict[str, object] = {}

    def fake_open(request, *, timeout, endpoint):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["timeout"] = timeout
        captured["endpoint"] = endpoint
        return response

    monkeypatch.setenv("BHM_CALLER_TOKEN", "t" * 32)
    monkeypatch.setattr(MODULE, "open_local_url", fake_open)

    result = MODULE.rest_call(
        "http://127.0.0.1:8000",
        "GET",
        "/bhm/memories",
        None,
        2.5,
        {"project": "blackholememory", "limit": 5},
    )

    assert result == {"ok": True}
    assert captured["url"] == "http://127.0.0.1:8000/bhm/memories?project=blackholememory&limit=5"
    assert captured["endpoint"] == "http://127.0.0.1:8000"
    assert captured["timeout"] == 2.5
    assert response.limits == [MODULE.MAX_REST_RESPONSE_BYTES + 1]
    assert captured["headers"]["Authorization"] == f"Bearer {'t' * 32}"


def test_rest_call_rejects_non_local_base_before_open(monkeypatch):
    called = False

    def fail_open(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("transport must not open an external endpoint")

    monkeypatch.setattr(MODULE, "open_local_url", fail_open)
    with pytest.raises(MODULE.SoftFail, match="endpoint rejected"):
        MODULE.rest_call("https://example.com", "GET", "/health/ready", None, 1.0)
    assert called is False


def test_rest_call_fails_closed_on_oversized_response(monkeypatch):
    response = _Response(b"x" * (MODULE.MAX_REST_RESPONSE_BYTES + 1))
    monkeypatch.setattr(MODULE, "open_local_url", lambda *_args, **_kwargs: response)
    with pytest.raises(MODULE.SoftFail, match="bounded"):
        MODULE.rest_call("http://127.0.0.1:8000", "GET", "/health/ready", None, 1.0)


def test_open_local_url_rejects_request_origin_mismatch():
    request = MODULE.request.Request("http://127.0.0.2:8000/health/ready")
    with pytest.raises(LocalEndpointError, match="differs"):
        MODULE.open_local_url(request, timeout=1.0, endpoint="http://127.0.0.1:8000")
