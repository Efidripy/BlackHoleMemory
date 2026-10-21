from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from blackholememory import app as bhm_app
from blackholememory.mcp_streamable_http import BhmStreamableHttpGateway

ADMIN = "operator-capability-0000000000000001"
BASE_HEADERS = {
    "accept": "application/json, text/event-stream",
    "authorization": "Bearer bhm-test-caller-token-0000000000000001",
    "content-type": "application/json",
}
SECRETLIKE = "api_key = sk-" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8"


@pytest.fixture
def writes(monkeypatch) -> list[Any]:
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", ADMIN)
    calls: list[Any] = []

    def fake_remember(request) -> dict[str, Any]:
        calls.append(request)
        return {
            "source_id": "mem_bhm_test_operator",
            "project": request.project,
            "memory_type": request.type,
            "memory_class": "episodic",
            "created_at": "2026-10-21T00:00:00Z",
            "metadata": {"raw_title": "t"},
        }

    monkeypatch.setattr(bhm_app, "_remember_live_memory", fake_remember)
    return calls


async def _post(client: httpx.AsyncClient, message: dict[str, Any], *, session_id: str = "", admin: str | None = None):
    headers = dict(BASE_HEADERS)
    if session_id:
        headers["mcp-protocol-version"] = "2025-06-18"
        headers["mcp-session-id"] = session_id
    if admin is not None:
        headers["x-bhm-admin-capability"] = admin
    return await client.post("/mcp", headers=headers, json=message)


async def _session(client: httpx.AsyncClient, *, admin: str | None = None) -> str:
    response = await _post(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
        },
        admin=admin,
    )
    assert response.status_code == 200
    session_id = response.headers["mcp-session-id"]
    ready = await _post(client, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, session_id=session_id, admin=admin)
    assert ready.status_code == 202
    return session_id


async def _remember(client, session_id, *, admin=None, arguments=None, meta=None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "name": "bhm_remember",
        "arguments": arguments if arguments is not None else {"content": "durable fact", "project": "blackholememory"},
    }
    if meta is not None:
        params["_meta"] = meta
    response = await _post(client, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params}, session_id=session_id, admin=admin)
    assert response.status_code == 200
    return response.json()


def _structured(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["result"].get("structuredContent") or json.loads(payload["result"]["content"][0]["text"])


async def _exercise(scenario) -> None:
    gateway = BhmStreamableHttpGateway(bhm_app._handle_mcp_gateway_jsonrpc_async, server_version="test")
    async with gateway.run():
        transport = httpx.ASGITransport(app=gateway.asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
            await scenario(client)


def test_valid_operator_header_writes_through_rest_path(writes):
    async def scenario(client):
        session_id = await _session(client, admin=ADMIN)
        payload = await _remember(client, session_id, admin=ADMIN, arguments={"content": "durable fact", "concepts": None, "files": None})
        body = _structured(payload)
        assert body["success"] is True
        assert body["memory"]["id"] == "mem_bhm_test_operator"
        assert ADMIN not in json.dumps(payload)

    asyncio.run(_exercise(scenario))
    assert len(writes) == 1
    assert writes[0].concepts == [] and writes[0].files == []


@pytest.mark.parametrize("admin", [None, "wrong-capability"])
def test_missing_or_wrong_header_is_rejected_without_write(writes, admin):
    async def scenario(client):
        session_id = await _session(client)
        body = _structured(await _remember(client, session_id, admin=admin))
        assert body["code"] == "ingress_admission_rejected"
        assert body["operator_header"] == ("absent" if admin is None else "invalid")
        assert "wrong-capability" not in json.dumps(body)

    asyncio.run(_exercise(scenario))
    assert writes == []


def test_capability_in_meta_or_arguments_is_not_enough(writes):
    async def scenario(client):
        session_id = await _session(client)
        via_meta = await _remember(client, session_id, meta={"bhm_admin_capability": ADMIN})
        assert _structured(via_meta)["code"] == "ingress_admission_rejected"
        via_args = await _remember(client, session_id, arguments={"content": "x", "capability": ADMIN})
        assert "error" in via_args or via_args["result"]["isError"] is True

    asyncio.run(_exercise(scenario))
    assert writes == []


def test_secret_like_content_is_rejected_for_operator(writes):
    async def scenario(client):
        session_id = await _session(client, admin=ADMIN)
        payload = await _remember(client, session_id, admin=ADMIN, arguments={"content": SECRETLIKE})
        assert payload["result"]["isError"] is True
        assert _structured(payload)["code"] == "ingress_admission_rejected"

    asyncio.run(_exercise(scenario))
    assert writes == []


def test_operator_bit_does_not_leak_between_requests_of_one_session(writes):
    async def scenario(client):
        session_id = await _session(client, admin=ADMIN)
        assert _structured(await _remember(client, session_id, admin=ADMIN))["success"] is True
        assert _structured(await _remember(client, session_id))["code"] == "ingress_admission_rejected"

    asyncio.run(_exercise(scenario))
    assert len(writes) == 1
