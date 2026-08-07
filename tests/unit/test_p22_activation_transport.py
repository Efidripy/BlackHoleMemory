from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from blackholememory.local_endpoint_policy import LocalEndpointError
from blackholememory.resource_limits import QDRANT_OPERATOR_HTTP_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "validate_bhm_p22_activation",
    ROOT / "scripts" / "validate-bhm-p22-activation.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        assert limit == 256 * 1024 + 1
        return b'{"result":{"collections":[{"name":"bhm_local_memory_demo"}]}}'


def test_qdrant_collections_uses_local_bounded_transport(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_open(request, *, timeout):
        calls["url"] = request.full_url
        calls["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(MODULE, "open_local_url", fake_open)
    assert MODULE.qdrant_collections("http://127.0.0.1:6333") == {
        "ok": True,
        "collections": [{"name": "bhm_local_memory_demo"}],
    }
    assert calls == {
        "url": "http://127.0.0.1:6333/collections",
        "timeout": QDRANT_OPERATOR_HTTP_TIMEOUT_SECONDS,
    }


def test_qdrant_collections_rejects_non_local_endpoint() -> None:
    with pytest.raises(LocalEndpointError, match="local-only"):
        MODULE.qdrant_collections("https://example.com")
