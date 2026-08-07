from __future__ import annotations

import pytest

from blackholememory.agents.developer_agent import BHMAgentExecutor
from blackholememory.langgraph_contract import SCHEMA_VERSION
from blackholememory.langgraph_contract import build_langgraph_contract


def test_multi_step_graph_is_explicitly_ephemeral_without_checkpointer() -> None:
    contract = build_langgraph_contract(
        "developer-agent",
        purpose="code generation",
        multi_step=True,
    )

    assert contract["schema_version"] == SCHEMA_VERSION
    assert contract["multi_step"] is True
    assert contract["mode"] == "ephemeral"
    assert contract["resumable"] is False
    assert contract["checkpointer_bound"] is False
    assert contract["status"] == "aligned"


def test_resumable_graph_requires_explicit_checkpointer() -> None:
    with pytest.raises(ValueError, match="requires_checkpointer"):
        build_langgraph_contract(
            "developer-agent",
            purpose="code generation",
            multi_step=True,
            resumable=True,
        )


def test_developer_graph_exposes_non_resumable_contract_by_default() -> None:
    compiled = BHMAgentExecutor(hypothesis_count=1).build_langgraph()

    contract = compiled.bhm_langgraph_contract
    assert contract["graph_id"] == "developer-agent"
    assert contract["multi_step"] is True
    assert contract["resumable"] is False
    assert contract["checkpointer_bound"] is False
