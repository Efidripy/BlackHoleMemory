from __future__ import annotations

import asyncio

import pytest
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent

from blackholememory import app as bhm_app
from blackholememory.mcp_protocol_contract import SUPPORTED_PROTOCOL_VERSIONS
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES


def _request(method: str, request_id: int, params: dict | None = None) -> dict | None:
    return asyncio.run(
        bhm_app._handle_mcp_gateway_jsonrpc_async(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
    )


def _notification(method: str, params: dict | None = None) -> dict | None:
    return asyncio.run(
        bhm_app._handle_mcp_gateway_jsonrpc_async(
            {"jsonrpc": "2.0", "method": method, "params": params or {}}
        )
    )


@pytest.mark.parametrize("protocol_version", SUPPORTED_PROTOCOL_VERSIONS)
def test_initialize_negotiation_advertises_supported_capabilities(protocol_version):
    response = _request(
        "initialize",
        1,
        {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "codex-desktop-regression", "version": "26.707.91948"},
        },
    )

    assert response is not None
    assert response["result"]["protocolVersion"] == protocol_version
    assert set(response["result"]["capabilities"]) == {"tools", "resources", "prompts"}


def test_current_codex_handshake_initializes_then_lists_exact_core_catalog():
    initialize = _request(
        "initialize",
        20,
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "Codex Desktop", "version": "26.707.91948"},
        },
    )
    initialized = _notification("notifications/initialized")
    catalog = _request("tools/list", 21)
    templates = _request("resources/templates/list", 22)

    assert initialize is not None
    assert initialize["result"]["protocolVersion"] == "2025-06-18"
    assert initialized is None
    assert catalog is not None
    tools = catalog["result"]["tools"]
    assert len(tools) == len(CORE_TOOL_NAMES)
    assert len({tool["name"] for tool in tools}) == len(CORE_TOOL_NAMES)
    assert all("inputSchema" in tool for tool in tools)
    assert all("fn" not in tool for tool in tools)
    assert templates == {"jsonrpc": "2.0", "id": 22, "result": {"resourceTemplates": []}}


def test_tools_call_serializes_fastmcp_tool_result(monkeypatch):
    from blackholememory import bhm_mcp

    async def fake_call_tool(name, arguments):
        assert name == "bhm_health"
        assert arguments == {}
        return ToolResult(
            content=[TextContent(type="text", text="ok")],
            structured_content={"ready": True},
            meta={"source": "test"},
        )

    monkeypatch.delenv("BHM_MCP_SURFACE", raising=False)
    monkeypatch.setattr(bhm_mcp.mcp, "call_tool", fake_call_tool)
    response = _request(
        "tools/call",
        23,
        {"name": "bhm_health", "arguments": {}},
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": 23,
        "result": {
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": {"ready": True},
            "_meta": {"source": "test"},
            "isError": False,
        },
    }


def test_initialize_rejects_unsupported_protocol_version_fail_closed():
    response = _request("initialize", 2, {"protocolVersion": "2099-01-01"})

    assert response is not None
    assert response["error"]["code"] == -32602
    assert "Unsupported MCP protocol version" in response["error"]["message"]


def test_initialized_and_cancelled_notifications_are_no_response():
    assert _notification("notifications/initialized") is None
    assert _notification("notifications/cancelled", {"requestId": 10, "reason": "operator stop"}) is None
    assert _notification("exit") is None


def test_ping_and_empty_resource_prompt_lists_are_conformant():
    ping = _request("ping", 3)
    resources = _request("resources/list", 4)
    templates = _request("resources/templates/list", 5)
    prompts = _request("prompts/list", 6)

    assert ping == {"jsonrpc": "2.0", "id": 3, "result": {}}
    assert resources == {"jsonrpc": "2.0", "id": 4, "result": {"resources": []}}
    assert templates == {"jsonrpc": "2.0", "id": 5, "result": {"resourceTemplates": []}}
    assert prompts == {"jsonrpc": "2.0", "id": 6, "result": {"prompts": []}}


def test_shutdown_is_a_structured_graceful_close_response():
    response = _request("shutdown", 6)

    assert response == {"jsonrpc": "2.0", "id": 6, "result": {}}


def test_cancel_request_and_unknown_method_are_structured_fail_closed_errors():
    cancel = _request("cancel", 7)
    unknown = _request("bhm/unsupported", 8)

    assert cancel is not None and cancel["error"]["code"] == -32601
    assert unknown is not None and unknown["error"]["code"] == -32601
