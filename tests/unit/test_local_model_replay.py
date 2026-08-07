from __future__ import annotations

import pytest
import httpx

from blackholememory.local_model_replay import DEFAULT_MODES
from blackholememory.local_model_replay import LOCAL_MODEL_REPLAY_DEFAULT_CASE_COUNT
from blackholememory.local_model_replay import LOCAL_MODEL_REPLAY_DEFAULT_REPEAT_COUNT
from blackholememory.local_model_replay import LOCAL_MODEL_REPLAY_EXPECTED_CALLS
from blackholememory.local_model_replay import run_local_model_replay
from blackholememory.local_model_replay import _bounded_json_response


def test_local_model_replay_rejects_non_local_or_tool_execution():
    with pytest.raises(ValueError, match="loopback/private"):
        import asyncio

        asyncio.run(run_local_model_replay(case_count=100, repeats=1, base_url="https://example.com/v1"))
    with pytest.raises(ValueError, match="tool_budget=0"):
        import asyncio

        asyncio.run(run_local_model_replay(case_count=100, repeats=1, tool_budget=1))


def test_local_model_replay_contract_defaults_are_explicit():
    assert DEFAULT_MODES == ("file-only", "bhm-full")
    assert LOCAL_MODEL_REPLAY_DEFAULT_CASE_COUNT == 111
    assert LOCAL_MODEL_REPLAY_DEFAULT_REPEAT_COUNT == 3
    assert LOCAL_MODEL_REPLAY_DEFAULT_CASE_COUNT * LOCAL_MODEL_REPLAY_DEFAULT_REPEAT_COUNT * len(DEFAULT_MODES) == LOCAL_MODEL_REPLAY_EXPECTED_CALLS == 666


def test_local_model_replay_transport_policy_bounds_json_response() -> None:
    response = httpx.Response(200, headers={"content-length": "2"}, content=b"{}")
    assert _bounded_json_response(response) == {}

    oversized = httpx.Response(200, headers={"content-length": "999999999"}, content=b"{}")
    with pytest.raises(ValueError, match="bounded limit"):
        _bounded_json_response(oversized)
