from __future__ import annotations

from blackholememory.agents import developer_agent


def _tool_names(specs: list[dict]) -> set[str]:
    return {str(spec.get("function", {}).get("name") or "") for spec in specs}


def test_swarm_qa_tool_specs_exclude_host_shell() -> None:
    names = _tool_names(developer_agent._swarm_qa_tool_specs())

    assert names == set(developer_agent.SWARM_QA_TOOL_NAMES)
    assert not developer_agent._RETIRED_SWARM_MODEL_TOOLS & names


def test_tool_catalog_and_role_policy_are_exactly_synchronized() -> None:
    assert _tool_names(developer_agent._swarm_tool_specs()) == set(developer_agent._SWARM_TOOL_ALLOWED_ROLES)
    assert "bash" not in _tool_names(developer_agent._swarm_tool_specs())


def test_developer_specs_exclude_infrastructure_and_mcp_tools() -> None:
    names = _tool_names(developer_agent._swarm_developer_tool_specs())

    assert {"python", "analyze_screenshot", "tool_get_file_outline", "tool_get_symbol_definition"} <= names
    assert not developer_agent._RETIRED_SWARM_MODEL_TOOLS & names


def test_legacy_bash_tool_call_is_rejected_before_subprocess(monkeypatch) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("model-selected host subprocess must not run")

    monkeypatch.setattr(developer_agent.subprocess, "run", fail_if_called)

    result = developer_agent._execute_swarm_tool_call(
        {"id": "legacy-bash", "name": "bash", "args": {"command": "echo unsafe"}},
        lambda _code, _timeout: {"success": True, "exit_code": 0, "stdout": "", "stderr": ""},
        current_assignee="qa",
    )

    assert result["success"] is False
    assert result["exit_code"] == 126
    assert result["stderr"] == "host shell is disabled for model-selected tool calls"


def test_non_json_tool_arguments_fail_closed() -> None:
    call = developer_agent._normalize_tool_call(
        {"id": "malformed", "name": "python", "arguments": "print('not json')"}
    )

    assert call is not None
    assert call["args"] == {}


def test_model_tool_response_cannot_widen_qa_allowlist() -> None:
    result = developer_agent._execute_swarm_tool_call(
        {"id": "unknown", "name": "write_file", "args": {"path": "x", "content": "y"}},
        lambda _code, _timeout: {"success": True, "exit_code": 0, "stdout": "", "stderr": ""},
        current_assignee="qa",
    )

    assert result["success"] is False
    assert result["exit_code"] == 127
    assert result["stderr"] == "unknown tool: write_file"


def test_unauthorized_mcp_docker_never_calls_provider(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(developer_agent.subprocess, "run", lambda *_args, **_kwargs: calls.append({}))

    result = developer_agent._execute_swarm_tool_call(
        {
            "id": "developer-docker",
            "name": "mcp_docker",
            "args": {"action": "docker_info", "current_assignee": "qa"},
        },
        lambda _code, _timeout: {"success": True, "exit_code": 0, "stdout": "", "stderr": ""},
        current_assignee="qa",
    )

    assert calls == []
    assert result["exit_code"] == 126
    assert "model-selected tool calls" in result["stderr"]


def test_mixed_allowed_and_denied_tool_batch_executes_nothing() -> None:
    sandbox_calls: list[str] = []

    def sandbox(code: str, _timeout: int) -> dict:
        sandbox_calls.append(code)
        return {"success": True, "exit_code": 0, "stdout": "", "stderr": ""}

    executor = developer_agent.BHMAgentExecutor(hypothesis_count=1, sandbox_runner=sandbox)
    result = executor.tools_node(
        {
            "task_id": "policy-batch",
            "current_assignee": "qa",
            "tool_calls": [
                {"id": "allowed-python", "name": "python", "args": {"code": "print('safe')"}},
                {"id": "retired-bash", "name": "bash", "args": {"command": "echo unsafe"}},
            ],
        }
    )

    assert sandbox_calls == []
    assert [item["exit_code"] for item in result["tool_results"]] == [125, 126]
