from __future__ import annotations

import socket
import urllib.error

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


def test_mem0_qdrant_health_exposes_redacted_connection_refused_diagnostics(monkeypatch) -> None:
    mem0_adapter._reset_qdrant_health_diagnostics_for_tests()

    def refused(*_args, **_kwargs):
        raise urllib.error.URLError(ConnectionRefusedError(10061, "private endpoint detail"))

    monkeypatch.setattr(mem0_adapter, "open_local_url", refused)
    assert mem0_adapter._remote_qdrant_available() is False

    diagnostics = mem0_adapter.qdrant_health_diagnostics()
    assert diagnostics["schema_version"] == "bhm.qdrant-health-diagnostic.v1"
    assert diagnostics["last_failure_class"] == "connection_refused"
    assert diagnostics["negative_cache_active"] is True
    assert diagnostics["consecutive_failures"] == 1
    assert diagnostics["total_failures"] == 1
    assert "private endpoint detail" not in str(diagnostics)


def test_mem0_qdrant_health_exposes_redacted_http_failure_diagnostics(monkeypatch) -> None:
    mem0_adapter._reset_qdrant_health_diagnostics_for_tests()

    class UnavailableResponse:
        status = 503

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit: int) -> bytes:
            return b"unavailable"

    monkeypatch.setattr(mem0_adapter, "open_local_url", lambda *_args, **_kwargs: UnavailableResponse())
    assert mem0_adapter._remote_qdrant_available() is False

    diagnostics = mem0_adapter.qdrant_health_diagnostics()
    assert diagnostics["last_failure_class"] == "http_503"
    assert diagnostics["last_http_status"] == 503


def test_mem0_qdrant_health_exposes_redacted_timeout_diagnostics(monkeypatch) -> None:
    mem0_adapter._reset_qdrant_health_diagnostics_for_tests()

    def timed_out(*_args, **_kwargs):
        raise urllib.error.URLError(socket.timeout("private timeout detail"))

    monkeypatch.setattr(mem0_adapter, "open_local_url", timed_out)
    assert mem0_adapter._remote_qdrant_available() is False

    diagnostics = mem0_adapter.qdrant_health_diagnostics()
    assert diagnostics["last_failure_class"] == "timeout"
    assert diagnostics["last_http_status"] is None
    assert "private timeout detail" not in str(diagnostics)


def test_mem0_qdrant_health_success_resets_consecutive_failures(monkeypatch) -> None:
    mem0_adapter._reset_qdrant_health_diagnostics_for_tests()

    def refused(*_args, **_kwargs):
        raise urllib.error.URLError(ConnectionRefusedError(10061))

    monkeypatch.setattr(mem0_adapter, "open_local_url", refused)
    assert mem0_adapter._remote_qdrant_available() is False

    monkeypatch.setattr(mem0_adapter, "_qdrant_unavailable_until", 0.0)
    monkeypatch.setattr(mem0_adapter, "open_local_url", lambda *_args, **_kwargs: _HealthResponse())
    assert mem0_adapter._remote_qdrant_available() is True

    diagnostics = mem0_adapter.qdrant_health_diagnostics()
    assert diagnostics["last_failure_class"] is None
    assert diagnostics["last_http_status"] == 200
    assert diagnostics["consecutive_failures"] == 0
    assert diagnostics["total_failures"] == 1
    assert diagnostics["total_successes"] == 1
    assert "127.0.0.1" not in str(diagnostics)


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
