from __future__ import annotations

import pytest

from blackholememory.local_model_replay import DEFAULT_MODES
from blackholememory.local_model_replay import run_local_model_replay


def test_local_model_replay_rejects_non_local_or_tool_execution():
    with pytest.raises(ValueError, match="loopback/private"):
        import asyncio

        asyncio.run(run_local_model_replay(case_count=100, repeats=1, base_url="https://example.com/v1"))
    with pytest.raises(ValueError, match="tool_budget=0"):
        import asyncio

        asyncio.run(run_local_model_replay(case_count=100, repeats=1, tool_budget=1))


def test_local_model_replay_contract_defaults_are_explicit():
    assert DEFAULT_MODES == ("file-only", "bhm-full")
