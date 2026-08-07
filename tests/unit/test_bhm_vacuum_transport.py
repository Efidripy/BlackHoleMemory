from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from blackholememory.local_endpoint_policy import LocalEndpointError


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "bhm_vacuum",
    ROOT / "scripts" / "bhm_vacuum.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class _Response:
    status = 200

    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        assert limit == 256 * 1024 + 1
        return self.payload


def test_request_json_uses_local_bounded_transport(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_open(request, *, timeout):
        calls["url"] = request.full_url
        calls["method"] = request.get_method()
        calls["timeout"] = timeout
        return _Response(b'{"result":{"collections":[]}}')

    monkeypatch.setattr(MODULE, "open_local_url", fake_open)
    assert MODULE.request_json("http://127.0.0.1:6333", "GET", "/collections", timeout=7) == {
        "result": {"collections": []}
    }
    assert calls == {
        "url": "http://127.0.0.1:6333/collections",
        "method": "GET",
        "timeout": 7.0,
    }


def test_qdrant_operator_timeout_is_registry_bounded() -> None:
    assert MODULE.DEFAULT_TIMEOUT_SECONDS == 30.0
    assert MODULE.bounded_qdrant_operator_timeout(7) == 7.0
    assert MODULE.bounded_qdrant_operator_timeout(999) == 30.0
    assert MODULE.bounded_qdrant_operator_timeout(0) == 1.0
    with pytest.raises(ValueError, match="finite"):
        MODULE.bounded_qdrant_operator_timeout(float("inf"))


def test_request_json_rejects_non_local_endpoint() -> None:
    with pytest.raises(LocalEndpointError, match="local-only"):
        MODULE.request_json("https://example.com", "GET", "/collections", timeout=7)


def test_request_json_fails_closed_on_oversized_response(monkeypatch) -> None:
    monkeypatch.setattr(MODULE, "open_local_url", lambda *_args, **_kwargs: _Response(b"x" * (256 * 1024 + 1)))
    with pytest.raises(LocalEndpointError, match="bounded limit"):
        MODULE.request_json("http://127.0.0.1:6333", "GET", "/collections", timeout=7)
