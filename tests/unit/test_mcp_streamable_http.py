from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

import httpx
from mcp import types

from blackholememory import app as bhm_app
from blackholememory.mcp_streamable_http import BhmStreamableHttpGateway
from blackholememory.mcp_streamable_http import HttpMcpSessionRegistry
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES


BASE_HEADERS = {
    "accept": "application/json, text/event-stream",
    "authorization": "Bearer bhm-test-caller-token-0000000000000001",
    "content-type": "application/json",
}


def _post(
    client: httpx.AsyncClient,
    message: dict[str, Any],
    *,
    session_id: str = "",
):
    headers = dict(BASE_HEADERS)
    if session_id:
        headers["mcp-protocol-version"] = "2025-06-18"
        headers["mcp-session-id"] = session_id
    return client.post("/mcp", headers=headers, json=message)


async def _initialize(client: httpx.AsyncClient, request_id: int) -> tuple[dict[str, Any], str]:
    response = await _post(
        client,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "Codex Desktop", "version": "26.707.91948"},
            },
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["protocolVersion"] == "2025-06-18"
    session_id = response.headers["mcp-session-id"]
    initialized = await _post(
        client,
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        session_id=session_id,
    )
    assert initialized.status_code == 202
    return payload, session_id


def test_streamable_http_matches_core_catalog_and_recovers_after_disconnect():
    async def exercise() -> None:
        gateway = BhmStreamableHttpGateway(
            bhm_app._handle_mcp_gateway_jsonrpc_async,
            server_version="test",
        )
        direct = await bhm_app._handle_mcp_gateway_jsonrpc_core(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        assert direct is not None
        expected = direct["result"]["tools"]

        async with gateway.run():
            first_transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=first_transport, base_url="http://127.0.0.1:8000") as client:
                _, session_id = await _initialize(client, 10)

            recovered_transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=recovered_transport, base_url="http://127.0.0.1:8000") as client:
                response = await _post(
                    client,
                    {"jsonrpc": "2.0", "id": 20, "method": "tools/list", "params": {}},
                    session_id=session_id,
                )
                assert response.status_code == 200
                tools = response.json()["result"]["tools"]
                assert len(tools) == len(CORE_TOOL_NAMES)
                assert tools == expected
                assert gateway.sessions.snapshot()["status"] == "attached"

                deleted = await client.delete(
                    "/mcp",
                    headers={
                        "authorization": BASE_HEADERS["authorization"],
                        "mcp-session-id": session_id,
                        "mcp-protocol-version": "2025-06-18",
                    },
                )
                assert deleted.status_code == 200
                stale = await _post(
                    client,
                    {"jsonrpc": "2.0", "id": 21, "method": "tools/list", "params": {}},
                    session_id=session_id,
                )
                assert stale.status_code == 404
                assert gateway.sessions.snapshot()["status"] == "detached"

    asyncio.run(exercise())


def test_streamable_http_tool_call_uses_shared_dispatcher():
    calls: list[dict[str, Any]] = []

    async def dispatch(message: dict[str, Any]) -> dict[str, Any]:
        calls.append(message)
        if message["method"] == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {
                            "name": "bhm_echo",
                            "description": "Echo a value.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                                "required": ["value"],
                            },
                        }
                    ]
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"content": [{"type": "text", "text": message["params"]["arguments"]["value"]}]},
        }

    async def exercise() -> None:
        gateway = BhmStreamableHttpGateway(dispatch, server_version="test")
        async with gateway.run():
            transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
                _, session_id = await _initialize(client, 1)
                response = await _post(
                    client,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "bhm_echo", "arguments": {"value": "ahoy"}},
                    },
                    session_id=session_id,
                )
                assert response.status_code == 200
                assert response.json()["result"]["content"] == [{"type": "text", "text": "ahoy"}]
                session = gateway.contract_snapshot()["sessions"]["sessions"][0]
                assert session["state"] == "healthy"
                assert session["catalog_hash"]
                assert session["contract_digest"]
                assert session["tool_count"] == 1
                assert session["contract_state"] == "aligned"
                assert session["lease_remaining_seconds"] > 0
                contract = gateway.contract_snapshot()
                assert contract["contract_schema_version"] == "bhm.mcp.transport-contract.v1"
                assert contract["contract_digest"] == session["contract_digest"]
        assert [item["method"] for item in calls] == ["tools/list", "tools/list", "tools/call"]

    asyncio.run(exercise())


def test_streamable_http_contract_drift_is_visible_without_rejecting_session():
    registry = HttpMcpSessionRegistry(idle_seconds=30)
    registry.register(
        "session-1",
        client_id="Codex Desktop",
        client_version="test",
        protocol_version="2025-06-18",
    )
    registry.touch(
        "session-1",
        method="tools/list",
        catalog_hash="catalog-a",
        contract_digest="contract-a",
        tool_count=35,
    )
    snapshot = registry.snapshot(current_contract_digest="contract-b")
    assert snapshot["contract_drift_count"] == 1
    assert snapshot["sessions"][0]["contract_state"] == "drifted"
    assert snapshot["sessions"][0]["state"] == "catalog_ready"
    assert snapshot["sessions"][0]["tool_count"] == 35


def test_streamable_http_tool_call_is_attached_during_dispatch():
    observed: list[str] = []
    gateway_ref: dict[str, BhmStreamableHttpGateway] = {}

    async def dispatch(message: dict[str, Any]) -> dict[str, Any]:
        if message["method"] == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"tools": []},
            }
        observed.append(str(gateway_ref["gateway"].sessions.snapshot()["status"]))
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }

    async def exercise() -> None:
        gateway = BhmStreamableHttpGateway(dispatch, server_version="test")
        gateway_ref["gateway"] = gateway
        async with gateway.run():
            transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
                _, session_id = await _initialize(client, 30)
                response = await _post(
                    client,
                    {
                        "jsonrpc": "2.0",
                        "id": 31,
                        "method": "tools/call",
                        "params": {"name": "bhm_echo", "arguments": {}},
                    },
                    session_id=session_id,
                )
                assert response.status_code == 200
        assert observed == ["attached"]

    asyncio.run(exercise())


def test_streamable_http_rejected_requests_never_promote_or_renew_health():
    async def dispatch(message: dict[str, Any]) -> dict[str, Any]:
        if message["method"] == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"tools": [{"name": "bhm_echo", "inputSchema": {"type": "object"}}]},
            }
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }

    async def exercise() -> None:
        gateway = BhmStreamableHttpGateway(dispatch, server_version="test")
        async with gateway.run():
            transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
                _, session_id = await _initialize(client, 50)
                valid = await _post(
                    client,
                    {"jsonrpc": "2.0", "id": 51, "method": "tools/call", "params": {"name": "bhm_echo", "arguments": {}}},
                    session_id=session_id,
                )
                assert valid.status_code == 200
                before = gateway.sessions.snapshot()["sessions"][0]

                for index, overrides in enumerate(
                    (
                        {"accept": "application/json"},
                        {"mcp-protocol-version": "1999-01-01"},
                        {"content-type": "text/plain"},
                    ),
                    start=52,
                ):
                    headers = dict(BASE_HEADERS)
                    headers.update(overrides)
                    headers["mcp-session-id"] = session_id
                    response = await client.post(
                        "/mcp",
                        headers=headers,
                        content=b'{"jsonrpc":"2.0","id":%d,"method":"tools/call","params":{}}' % index,
                    )
                    assert response.status_code in {200, 400, 406}

                after = gateway.sessions.snapshot()["sessions"][0]
                assert after["state"] == "healthy"
                assert after["last_request"] == "tools/call"
                assert after["updated_at"] == before["updated_at"]

    asyncio.run(exercise())


def test_streamable_http_tool_call_rehydrates_expired_diagnostics_for_live_sdk_session():
    observed: list[str] = []
    gateway_ref: dict[str, BhmStreamableHttpGateway] = {}

    async def dispatch(message: dict[str, Any]) -> dict[str, Any]:
        if message["method"] == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {
                            "name": "bhm_echo",
                            "description": "Echo.",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            }
        observed.append(str(gateway_ref["gateway"].sessions.snapshot()["status"]))
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }

    async def exercise() -> None:
        gateway = BhmStreamableHttpGateway(dispatch, server_version="test")
        gateway_ref["gateway"] = gateway
        async with gateway.run():
            transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
                _, session_id = await _initialize(client, 32)
                with gateway.sessions._lock:
                    gateway.sessions._sessions[session_id].expires_at_monotonic = 0.0

                expired = gateway.sessions.snapshot()
                assert expired["status"] == "detached"
                assert expired["expired_count"] == 1
                assert session_id in gateway.manager._server_instances

                response = await _post(
                    client,
                    {
                        "jsonrpc": "2.0",
                        "id": 33,
                        "method": "tools/call",
                        "params": {"name": "bhm_echo", "arguments": {}},
                    },
                    session_id=session_id,
                )
                assert response.status_code == 200
                assert observed == ["attached"]

                current = gateway.sessions.snapshot()
                assert current["status"] == "attached"
                assert current["session_count"] == 1
                assert current["expired_count"] == 1
                session = current["sessions"][0]
                assert session["client_id"] == "sdk-session-rehydrated"
                assert session["client_version"] == "unknown"
                assert session["protocol_version"] == "2025-06-18"
                assert session["state"] == "healthy"
                assert session["last_request"] == "tools/call"
                assert session["contract_state"] == "aligned"
                assert session["tool_count"] == 1
                assert session_id not in str(current)

                unknown = await _post(
                    client,
                    {
                        "jsonrpc": "2.0",
                        "id": 34,
                        "method": "tools/call",
                        "params": {"name": "bhm_echo", "arguments": {}},
                    },
                    session_id="f" * 32,
                )
                assert unknown.status_code == 404
                after_unknown = gateway.sessions.snapshot()
                assert after_unknown["session_count"] == 1
                assert after_unknown["sessions"][0]["session_ref"] == session["session_ref"]

    asyncio.run(exercise())


def test_streamable_http_missing_protocol_header_uses_sdk_default_evidence():
    async def dispatch(message: dict[str, Any]) -> dict[str, Any]:
        if message["method"] == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"tools": [{"name": "bhm_echo", "inputSchema": {"type": "object"}}]},
            }
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }

    async def exercise() -> None:
        gateway = BhmStreamableHttpGateway(dispatch, server_version="test")
        async with gateway.run():
            transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
                _, session_id = await _initialize(client, 35)
                with gateway.sessions._lock:
                    gateway.sessions._sessions[session_id].expires_at_monotonic = 0.0

                headers = dict(BASE_HEADERS)
                headers["mcp-session-id"] = session_id
                response = await client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 36,
                        "method": "tools/call",
                        "params": {"name": "bhm_echo", "arguments": {}},
                    },
                )
                assert response.status_code == 200
                session = gateway.contract_snapshot()["sessions"]["sessions"][0]
                assert session["protocol_version"] == types.DEFAULT_NEGOTIATED_VERSION
                assert session["contract_state"] == "drifted"

    asyncio.run(exercise())


def test_streamable_http_records_transport_loss_and_recovers_with_new_session():
    async def dispatch(message: dict[str, Any]) -> dict[str, Any]:
        if message["method"] == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {"tools": [{"name": "bhm_echo", "description": "Echo.", "inputSchema": {"type": "object"}}]},
            }
        await asyncio.sleep(0)
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }

    async def exercise() -> None:
        gateway = BhmStreamableHttpGateway(dispatch, server_version="test")

        async def dropping_app(scope, receive, send):
            async def drop_body(event):
                if event.get("type") == "http.response.body":
                    raise ConnectionError("simulated client transport loss")
                await send(event)

            await gateway.asgi_app(scope, receive, drop_body)

        async with gateway.run():
            healthy_transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=healthy_transport, base_url="http://127.0.0.1:8000") as client:
                _, old_session_id = await _initialize(client, 40)

            failing_transport = httpx.ASGITransport(app=dropping_app)
            async with httpx.AsyncClient(transport=failing_transport, base_url="http://127.0.0.1:8000") as client:
                try:
                    await _post(
                        client,
                        {
                            "jsonrpc": "2.0",
                            "id": 41,
                            "method": "tools/call",
                            "params": {"name": "bhm_echo", "arguments": {}},
                        },
                        session_id=old_session_id,
                    )
                except BaseException:
                    pass

            lost = gateway.sessions.snapshot()
            old_ref = hashlib.sha256(old_session_id.encode()).hexdigest()[:12]
            old_row = next(row for row in lost["sessions"] if row["session_ref"] == old_ref)
            assert old_row["state"] == "pending"
            assert old_row["transport_loss_count"] >= 1
            assert old_row["last_transport_event"] in {"ConnectionError", "AssertionError"}

            reconnect_transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=reconnect_transport, base_url="http://127.0.0.1:8000") as client:
                _, new_session_id = await _initialize(client, 42)
                assert new_session_id != old_session_id
                response = await _post(
                    client,
                    {"jsonrpc": "2.0", "id": 43, "method": "tools/list", "params": {}},
                    session_id=new_session_id,
                )
                assert response.status_code == 200
                assert response.json()["result"]["tools"][0]["name"] == "bhm_echo"
                current = gateway.sessions.snapshot()
                new_ref = hashlib.sha256(new_session_id.encode()).hexdigest()[:12]
                new_row = next(row for row in current["sessions"] if row["session_ref"] == new_ref)
                assert new_row["state"] == "catalog_ready"
                assert new_row["contract_digest"] == old_row["contract_digest"]
                assert new_row["tool_count"] == old_row["tool_count"]

    asyncio.run(exercise())


def test_streamable_http_rejects_untrusted_origin():
    async def exercise() -> None:
        gateway = BhmStreamableHttpGateway(
            bhm_app._handle_mcp_gateway_jsonrpc_async,
            server_version="test",
        )
        async with gateway.run():
            transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
                response = await client.post(
                    "/mcp",
                    headers={**BASE_HEADERS, "origin": "https://attacker.example"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    },
                )
                assert response.status_code == 403

    asyncio.run(exercise())


def test_streamable_http_rejects_missing_bearer_before_session_creation():
    async def exercise() -> None:
        gateway = BhmStreamableHttpGateway(
            bhm_app._handle_mcp_gateway_jsonrpc_async,
            server_version="test",
        )
        async with gateway.run():
            transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
                response = await client.post(
                    "/mcp",
                    headers={"accept": BASE_HEADERS["accept"], "content-type": BASE_HEADERS["content-type"]},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    },
                )
                assert response.status_code == 401
                assert response.json()["detail"]["code"] == "caller_auth_required"
                assert gateway.sessions.snapshot()["session_count"] == 0

    asyncio.run(exercise())


def test_scoped_streamable_http_requires_explicit_project_only_for_tool_calls(monkeypatch):
    monkeypatch.setenv("BHM_CALLER_PROJECTS", "blackholememory")
    monkeypatch.setenv("BHM_CALLER_DEFAULT_PROJECT", "blackholememory")

    async def dispatch(message: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }

    async def exercise() -> None:
        gateway = BhmStreamableHttpGateway(dispatch, server_version="test")
        async with gateway.run():
            transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
                _, session_id = await _initialize(client, 1)
                omitted = await _post(
                    client,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "bhm_search", "arguments": {"query": "scope"}},
                    },
                    session_id=session_id,
                )
                explicit = await _post(
                    client,
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "bhm_search",
                            "arguments": {"query": "scope", "project": "blackholememory"},
                        },
                    },
                    session_id=session_id,
                )

                assert omitted.status_code == 403
                assert omitted.json()["detail"]["code"] == "caller_project_required"
                assert explicit.status_code == 200

    asyncio.run(exercise())


def test_streamable_http_tool_call_does_not_block_event_loop():
    async def dispatch(message: dict[str, Any]) -> dict[str, Any]:
        if message["method"] == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {
                            "name": "bhm_slow",
                            "description": "Bounded blocking fixture.",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            }
        time.sleep(0.25)
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"content": [{"type": "text", "text": "done"}]},
        }

    async def exercise() -> None:
        gateway = BhmStreamableHttpGateway(dispatch, server_version="test")
        async with gateway.run():
            transport = httpx.ASGITransport(app=gateway.asgi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
                _, session_id = await _initialize(client, 1)
                started = time.perf_counter()
                slow_call = asyncio.create_task(
                    _post(
                        client,
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "bhm_slow", "arguments": {}},
                        },
                        session_id=session_id,
                    )
                )
                await asyncio.sleep(0.03)
                assert time.perf_counter() - started < 0.15
                assert slow_call.done() is False
                response = await slow_call
                assert response.status_code == 200

    asyncio.run(exercise())
