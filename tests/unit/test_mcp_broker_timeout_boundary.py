from __future__ import annotations

from pathlib import Path

from blackholememory.resource_limits import MCP_BROKER_CAPACITY_WAIT_SECONDS
from blackholememory.resource_limits import MCP_BROKER_JOIN_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[2]


def test_mcp_broker_lifecycle_timeouts_are_registry_backed() -> None:
    assert MCP_BROKER_JOIN_TIMEOUT_SECONDS == 3.0
    assert MCP_BROKER_CAPACITY_WAIT_SECONDS == 0.2
    text = (ROOT / "src" / "blackholememory" / "infra" / "mcp_broker.py").read_text(encoding="utf-8")
    assert "MCP_BROKER_JOIN_TIMEOUT_SECONDS" in text
    assert "MCP_BROKER_CAPACITY_WAIT_SECONDS" in text
    assert "join(timeout=3.0)" not in text
    assert "wait(timeout=0.2)" not in text
