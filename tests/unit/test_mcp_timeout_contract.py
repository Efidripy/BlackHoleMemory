from __future__ import annotations

import pytest

from blackholememory.mcp_timeout_contract import McpTimeoutContract


def test_timeout_contract_keeps_startup_protocol_tool_and_provider_budgets_separate():
    contract = McpTimeoutContract(
        startup_seconds=15,
        api_probe_seconds=3,
        pipe_connect_seconds=1,
        initialize_seconds=11,
        catalog_seconds=13,
        tool_call_seconds=17,
        provider_warmup_seconds=5,
        max_concurrent_clients=4,
    )

    assert contract.response_timeout_seconds("initialize") == 11.0
    assert contract.response_timeout_seconds("tools/list") == 13.0
    assert contract.response_timeout_seconds("tools/call") == 17.0
    payload = contract.as_dict()
    assert payload["schema_version"] == "bhm.mcp.timeout-contract.v1"
    assert payload["scope"] == "bhm-only"
    assert payload["budgets"]["provider_warmup_seconds"] == 5.0
    assert payload["isolation"] == {
        "unrelated_mcp_wait": False,
        "max_concurrent_clients": 4,
        "shared_startup_lock": True,
    }


def test_timeout_contract_reads_existing_runtime_environment(monkeypatch):
    monkeypatch.setenv("BHM_MCP_READINESS_DEADLINE_MS", "12000")
    monkeypatch.setenv("BHM_MCP_API_PROBE_TIMEOUT_MS", "1500")
    monkeypatch.setenv("BHM_MCP_CONNECT_TIMEOUT_MS", "750")
    monkeypatch.setenv("BHM_MCP_INITIALIZE_TIMEOUT_SECONDS", "9")
    monkeypatch.setenv("BHM_MCP_CATALOG_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("BHM_MCP_TOOL_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("BHM_PROVIDER_READINESS_WAIT_SECONDS", "4")
    monkeypatch.setenv("BHM_MCP_BROKER_MAX_CLIENTS", "6")

    contract = McpTimeoutContract.from_env()
    assert contract.startup_seconds == 12.0
    assert contract.api_probe_seconds == 1.5
    assert contract.pipe_connect_seconds == 0.75
    assert contract.initialize_seconds == 9.0
    assert contract.catalog_seconds == 10.0
    assert contract.tool_call_seconds == 12.0
    assert contract.provider_warmup_seconds == 4.0
    assert contract.max_concurrent_clients == 6


def test_timeout_contract_rejects_unbounded_budget():
    with pytest.raises(ValueError, match="startup_seconds"):
        McpTimeoutContract(startup_seconds=121)

    with pytest.raises(ValueError, match="max_concurrent_clients"):
        McpTimeoutContract(max_concurrent_clients=65)
