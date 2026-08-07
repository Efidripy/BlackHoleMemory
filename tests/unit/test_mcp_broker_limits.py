from __future__ import annotations

import json
import socket
import threading
import time

from blackholememory.infra.mcp_broker import McpIpcBroker
from blackholememory.resource_limits import MCP_BROKER_CAPACITY_WAIT_SECONDS
from blackholememory.resource_limits import MCP_BROKER_JOIN_TIMEOUT_SECONDS


def _decode(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


def test_broker_rejects_oversized_frame_before_json_dispatch():
    broker = McpIpcBroker(max_frame_bytes=4096)

    raw = broker._dispatch_line(b"x" * 4097)

    assert raw is not None
    response = _decode(raw)
    assert response["error"]["code"] == -32002


def test_broker_times_out_slow_handler():
    broker = McpIpcBroker(dispatch_timeout_seconds=0.1)

    def slow_handler(_payload: dict) -> dict:
        time.sleep(0.25)
        return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

    broker._handler = slow_handler
    started = time.perf_counter()
    try:
        raw = broker._dispatch_line(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\n')
    finally:
        broker.close()
    elapsed = time.perf_counter() - started

    assert raw is not None
    response = _decode(raw)
    assert response["error"]["code"] == -32004
    assert elapsed < 0.2


def test_broker_bounds_oversized_response():
    broker = McpIpcBroker(max_frame_bytes=4096)
    broker._handler = lambda _payload: {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"blob": "x" * 5000},
    }

    try:
        raw = broker._dispatch_line(b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{}}\n')
    finally:
        broker.close()

    assert raw is not None
    response = _decode(raw)
    assert response["error"]["code"] == -32005


def test_broker_limits_have_safe_minimums():
    broker = McpIpcBroker(max_frame_bytes=1, client_timeout_seconds=0, dispatch_timeout_seconds=0)
    try:
        assert broker.max_frame_bytes == 4096
        assert broker.client_timeout_seconds == 0.1
        assert broker.dispatch_timeout_seconds == 0.1
    finally:
        broker.close()


def test_broker_lifecycle_waits_use_registry_bounds():
    assert MCP_BROKER_JOIN_TIMEOUT_SECONDS == 3.0
    assert MCP_BROKER_CAPACITY_WAIT_SECONDS == 0.2


def test_broker_propagates_connection_context_to_handler():
    broker = McpIpcBroker()
    seen: list[str | None] = []

    def handler(payload: dict) -> dict:
        seen.append(broker.current_connection_id)
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}}

    broker._handler = handler
    try:
        raw = broker._dispatch_line(
            b'{"jsonrpc":"2.0","id":3,"method":"ping","params":{}}\n',
            "pipe:context",
        )
    finally:
        broker.close()

    assert raw is not None
    assert _decode(raw)["result"] == {"ok": True}
    assert seen == ["pipe:context"]


def test_socket_client_idle_timeout_closes_connection():
    broker = McpIpcBroker(client_timeout_seconds=0.1)
    client, server = socket.socketpair()
    worker = threading.Thread(target=broker._handle_socket_client, args=(server,), daemon=True)
    worker.start()
    try:
        client.settimeout(1.0)
        assert client.recv(1) == b""
    finally:
        client.close()
        worker.join(timeout=1.0)
        broker.close()
    assert not worker.is_alive()
