from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.agents.developer_agent import BHMAgentExecutor
from blackholememory.langgraph_activation import LANGGRAPH_DURABLE_CHECKPOINT_ALLOW_AUTHORITATIVE_ENV
from blackholememory.langgraph_activation import LANGGRAPH_DURABLE_CHECKPOINT_CALLER_ID_ENV
from blackholememory.langgraph_activation import LANGGRAPH_DURABLE_CHECKPOINT_ENABLED_ENV
from blackholememory.langgraph_activation import LANGGRAPH_DURABLE_CHECKPOINT_SCHEMA_ENV
from blackholememory.langgraph_activation import create_durable_checkpoint_saver
from blackholememory.langgraph_activation import resolve_durable_checkpoint_activation
from blackholememory.langgraph_checkpoint import CHECKPOINT_SCHEMA_VERSION


def _enabled_env() -> dict[str, str]:
    return {
        LANGGRAPH_DURABLE_CHECKPOINT_ENABLED_ENV: "true",
        LANGGRAPH_DURABLE_CHECKPOINT_ALLOW_AUTHORITATIVE_ENV: "true",
        LANGGRAPH_DURABLE_CHECKPOINT_SCHEMA_ENV: CHECKPOINT_SCHEMA_VERSION,
        LANGGRAPH_DURABLE_CHECKPOINT_CALLER_ID_ENV: "local-operator",
    }


def test_activation_is_fail_closed_without_feature_request(tmp_path: Path) -> None:
    activation = resolve_durable_checkpoint_activation(runtime_dir=tmp_path, environ={})

    assert activation.enabled is False
    assert activation.reason == "durable_checkpoint_feature_disabled"
    assert activation.database_path == tmp_path / "live-memory" / "memories.sqlite3"


@pytest.mark.parametrize(
    ("removed", "reason"),
    [
        (LANGGRAPH_DURABLE_CHECKPOINT_ALLOW_AUTHORITATIVE_ENV, "durable_checkpoint_authoritative_ack_required"),
        (LANGGRAPH_DURABLE_CHECKPOINT_SCHEMA_ENV, "durable_checkpoint_schema_ack_required"),
        (LANGGRAPH_DURABLE_CHECKPOINT_CALLER_ID_ENV, "durable_checkpoint_caller_id_required"),
    ],
)
def test_activation_requires_each_independent_live_gate(
    tmp_path: Path,
    removed: str,
    reason: str,
) -> None:
    environ = _enabled_env()
    environ.pop(removed)

    activation = resolve_durable_checkpoint_activation(runtime_dir=tmp_path, environ=environ)

    assert activation.enabled is False
    assert activation.reason == reason


def test_activation_constructs_authoritative_saver_only_after_all_gates(tmp_path: Path) -> None:
    activation = resolve_durable_checkpoint_activation(runtime_dir=tmp_path, environ=_enabled_env())

    saver = create_durable_checkpoint_saver(
        project="blackholememory",
        task_id="task-1",
        session_id="session-1",
        activation=activation,
    )

    assert activation.enabled is True
    assert saver.enabled is True
    assert saver.allow_authoritative is True
    assert saver.database_path == tmp_path / "live-memory" / "memories.sqlite3"
    assert saver.database_path.exists()


def test_saver_construction_rejects_a_disabled_activation(tmp_path: Path) -> None:
    activation = resolve_durable_checkpoint_activation(runtime_dir=tmp_path, environ={})

    with pytest.raises(RuntimeError, match="feature_disabled"):
        create_durable_checkpoint_saver(
            project="blackholememory",
            task_id="task-1",
            session_id="session-1",
            activation=activation,
        )


def test_executor_uses_the_live_activation_only_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _enabled_env().items():
        monkeypatch.setenv(key, value)
    executor = BHMAgentExecutor(hypothesis_count=1, checkpoint_runtime_dir=tmp_path)

    saver, resumable, config = executor._resolve_execution_checkpoint(
        task_id="task-1",
        project="blackholememory",
        resumable=None,
    )

    assert resumable is True
    assert saver is not None
    assert config == {
        "configurable": {
            "thread_id": "task-1",
            "project": "blackholememory",
            "caller_id": "local-operator",
            "task_id": "task-1",
            "session_id": "task:task-1",
        }
    }


def test_executor_explicit_ephemeral_override_avoids_authoritative_saver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _enabled_env().items():
        monkeypatch.setenv(key, value)
    executor = BHMAgentExecutor(hypothesis_count=1, checkpoint_runtime_dir=tmp_path)

    saver, resumable, config = executor._resolve_execution_checkpoint(
        task_id="task-1",
        project="blackholememory",
        resumable=False,
    )

    assert (saver, resumable, config) == (None, False, None)
    assert not (tmp_path / "live-memory" / "memories.sqlite3").exists()
