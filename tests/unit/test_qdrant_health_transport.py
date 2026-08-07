from __future__ import annotations

from blackholememory import app as bhm_app
from blackholememory import mem0_adapter
from blackholememory.resource_limits import QDRANT_SDK_TIMEOUT_SECONDS
from blackholememory.resource_limits import QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS


class _HealthResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        return b"ok"


def test_app_qdrant_health_uses_local_bounded_transport(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_open(request, *, timeout: float):
        calls.append({"url": request.full_url, "timeout": timeout})
        return _HealthResponse()

    monkeypatch.setattr(bhm_app, "open_local_url", fake_open)
    assert bhm_app._qdrant_healthy_sync() is True
    assert calls[0]["url"].endswith("/healthz")
    assert calls[0]["timeout"] == bhm_app._QDRANT_HEALTH_TIMEOUT_SECONDS


def test_mem0_qdrant_health_uses_local_bounded_transport(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_open(request, *, timeout: float):
        calls.append({"url": request.full_url, "timeout": timeout})
        return _HealthResponse()

    monkeypatch.setattr(mem0_adapter, "open_local_url", fake_open)
    assert mem0_adapter._remote_qdrant_available() is True
    assert str(calls[0]["url"]).endswith("/healthz")
    assert calls[0]["timeout"] == QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS


def test_direct_qdrant_client_uses_shared_sdk_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(mem0_adapter, "QdrantClient", FakeClient)
    monkeypatch.setattr(mem0_adapter, "_qdrant_connection_config", lambda: {"url": "http://127.0.0.1:6333"})
    mem0_adapter.get_qdrant_client.cache_clear()
    try:
        mem0_adapter.get_qdrant_client()
    finally:
        mem0_adapter.get_qdrant_client.cache_clear()

    assert captured["url"] == "http://127.0.0.1:6333"
    assert captured["timeout"] == QDRANT_SDK_TIMEOUT_SECONDS


def test_mem0_qdrant_config_carries_shared_sdk_timeout(monkeypatch) -> None:
    monkeypatch.setattr(mem0_adapter, "_qdrant_connection_config", lambda: {"url": "http://127.0.0.1:6333"})

    config = mem0_adapter.build_mem0_config("bhm_local_memory_test")

    qdrant_config = config["vector_store"]["config"]
    assert qdrant_config["url"] == "http://127.0.0.1:6333"
    assert "timeout" not in qdrant_config
