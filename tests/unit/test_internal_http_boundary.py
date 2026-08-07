from __future__ import annotations

import math

import httpx
import pytest

from blackholememory.agents import developer_agent
from blackholememory.agents.developer_agent import BHMRestClient
from blackholememory.agents.developer_agent import LocalLLMClient
from blackholememory import bhm_mcp
from blackholememory.local_endpoint_policy import MAX_RESPONSE_BYTES
from blackholememory.local_endpoint_policy import LocalEndpointError
from blackholememory.resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import LLM_HTTP_TIMEOUT_SECONDS


def test_bhm_rest_client_requires_local_endpoint() -> None:
    with pytest.raises(ValueError, match="loopback/private"):
        BHMRestClient("https://example.com/api", timeout=3)


def test_bhm_rest_client_disables_proxy_redirects_and_bounds_response(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def post(self, path: str, *, json: dict[str, object]):
            captured["path"] = path
            captured["json"] = json
            class FakeResponse:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"ok": True}

            return FakeResponse()

    monkeypatch.setattr(developer_agent, "configured_caller_token", lambda: "test-token")
    monkeypatch.setattr(developer_agent.httpx, "Client", FakeClient)

    result = BHMRestClient("http://127.0.0.1:8000", timeout=3).post("/bhm/search", {"query": "x"})
    assert result == {"ok": True}
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    assert captured["timeout"] == 3.0

    oversized = httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(ValueError, match="bounded limit"):
        developer_agent._bounded_httpx_json(oversized)


def test_owned_agent_http_clients_clamp_and_reject_non_finite_timeouts() -> None:
    assert BHMRestClient("http://127.0.0.1:8000", timeout=999).timeout == float(BHM_INTERNAL_HTTP_TIMEOUT_SECONDS)
    assert LocalLLMClient("http://127.0.0.1:13666/v1", "test-model", "", 999).timeout == float(LLM_HTTP_TIMEOUT_SECONDS)

    with pytest.raises(ValueError, match="finite"):
        BHMRestClient("http://127.0.0.1:8000", timeout=math.inf)
    with pytest.raises(ValueError, match="finite"):
        LocalLLMClient("http://127.0.0.1:13666/v1", "test-model", "", math.nan)


def test_mcp_rest_client_disables_proxy_redirects_and_bounds_response(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        headers = {"content-length": "14"}
        content = b'{"ok":true}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def get(self, path: str, *, params=None):
            captured["path"] = path
            captured["params"] = params
            return FakeResponse()

    monkeypatch.setattr(bhm_mcp, "_read_process_or_user_env_value", lambda _key: "t" * 32)
    monkeypatch.setattr(bhm_mcp.httpx, "Client", FakeClient)

    assert bhm_mcp._get("/health/ready") == {"ok": True}
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    assert captured["timeout"] == float(BHM_INTERNAL_HTTP_TIMEOUT_SECONDS)
    assert captured["path"] == "/health/ready"

    oversized = httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(ValueError, match="bounded limit"):
        bhm_mcp._bounded_json_response(oversized)


def test_mcp_rest_client_rejects_remote_base_url_before_authenticated_transport(monkeypatch) -> None:
    monkeypatch.setattr(bhm_mcp, "DEFAULT_BASE_URL", "https://example.com/bhm")
    monkeypatch.setattr(bhm_mcp, "_read_process_or_user_env_value", lambda _key: "t" * 32)

    with pytest.raises(LocalEndpointError, match="loopback/private"):
        bhm_mcp._get("/health/ready")
