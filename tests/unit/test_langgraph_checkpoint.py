from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import END, START, StateGraph

from blackholememory.langgraph_checkpoint import SQLiteLangGraphCheckpointSaver
from blackholememory.langgraph_checkpoint import _redact
from blackholememory.langgraph_contract import build_langgraph_contract


class _State(TypedDict):
    value: int


def _saver(tmp_path: Path, **kwargs: object) -> SQLiteLangGraphCheckpointSaver:
    return SQLiteLangGraphCheckpointSaver(
        tmp_path / "disposable-checkpoints.sqlite3",
        project="BlackHoleMemory",
        caller_id="test-caller",
        task_id="test-task",
        session_id="test-session",
        enabled=True,
        **kwargs,
    )


def _config(thread_id: str = "thread-1", checkpoint_id: str | None = None) -> dict:
    configurable = {"thread_id": thread_id}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def _checkpoint(checkpoint_id: str, value: int | str) -> dict:
    checkpoint = empty_checkpoint()
    checkpoint.update(
        id=checkpoint_id,
        channel_values={"value": value},
        channel_versions={"value": checkpoint_id},
    )
    return checkpoint


def test_disabled_by_default_and_contract_is_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "disabled.sqlite3"
    saver = SQLiteLangGraphCheckpointSaver(
        path,
        project="p",
        caller_id="c",
        task_id="t",
        session_id="s",
    )

    assert saver.feature_state == "disabled"
    assert not path.exists()
    with pytest.raises(RuntimeError, match="feature_disabled"):
        saver.put(_config(), _checkpoint("0001", 1), {}, {"value": "0001"})
    with pytest.raises(ValueError, match="requires_checkpointer"):
        build_langgraph_contract(
            "developer-agent",
            purpose="test",
            multi_step=True,
            checkpointer=saver,
            resumable=True,
        )


def test_checkpoint_metadata_redacts_private_keys_and_credentials() -> None:
    redacted = _redact(
        {
            "private_key": "synthetic-private-key",
            "credential": "synthetic-credential",
            "nested": {"PRIVATE-KEY": "synthetic-nested-key"},
        }
    )

    assert redacted == {
        "private_key": "[REDACTED]",
        "credential": "[REDACTED]",
        "nested": {"PRIVATE-KEY": "[REDACTED]"},
    }


def test_authoritative_database_requires_explicit_operator_gate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="authoritative_sqlite"):
        SQLiteLangGraphCheckpointSaver(
            tmp_path / "live-memory" / "memories.sqlite3",
            project="p",
            caller_id="c",
            task_id="t",
            session_id="s",
            enabled=True,
        )


def test_disposable_database_rejects_hardlink_alias(tmp_path: Path) -> None:
    source = tmp_path / "authoritative.sqlite3"
    source.write_bytes(b"sqlite-fixture")
    alias = tmp_path / "alternate.sqlite3"
    try:
        alias.hardlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        SQLiteLangGraphCheckpointSaver(
            alias,
            project="p",
            caller_id="c",
            task_id="t",
            session_id="s",
            enabled=True,
        )


def test_disposable_database_rejects_symlink_alias(tmp_path: Path) -> None:
    source = tmp_path / "authoritative.sqlite3"
    source.write_bytes(b"sqlite-fixture")
    alias = tmp_path / "alternate.sqlite3"
    try:
        alias.symlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(OSError, match="symlink|reparse"):
        SQLiteLangGraphCheckpointSaver(
            alias,
            project="p",
            caller_id="c",
            task_id="t",
            session_id="s",
            enabled=True,
        )


def test_round_trip_parent_chain_and_reopen(tmp_path: Path) -> None:
    saver = _saver(tmp_path)
    root = saver.put(_config(), _checkpoint("0001", 1), {"step": 1}, {"value": "0001"})
    child = saver.put(root, _checkpoint("0002", 2), {"step": 2}, {"value": "0002"})

    loaded = saver.get_tuple(child)
    assert loaded is not None
    assert loaded.checkpoint["channel_values"] == {"value": 2}
    assert loaded.metadata["step"] == 2
    assert loaded.parent_config is not None
    assert loaded.parent_config["configurable"]["checkpoint_id"] == "0001"
    assert [item.config["configurable"]["checkpoint_id"] for item in saver.list(_config())] == ["0002", "0001"]

    reopened = _saver(tmp_path)
    assert reopened.get_tuple(_config(checkpoint_id="0001")) is not None
    assert reopened.get_tuple(_config()) is not None


def test_writes_are_idempotent_and_conflicts_fail_closed(tmp_path: Path) -> None:
    saver = _saver(tmp_path)
    config = saver.put(_config(), _checkpoint("0001", 1), {}, {"value": "0001"})
    saver.put_writes(config, [("value", 2)], "node", "path")
    saver.put_writes(config, [("value", 2)], "node", "path")
    loaded = saver.get_tuple(config)
    assert loaded is not None
    assert loaded.pending_writes == [("node", "value", 2)]
    with pytest.raises(ValueError, match="write_conflict"):
        saver.put_writes(config, [("value", 3)], "node", "path")


def test_scope_and_thread_isolation(tmp_path: Path) -> None:
    saver = _saver(tmp_path)
    saver.put(_config("thread-a"), _checkpoint("0001", 1), {}, {"value": "0001"})
    saver.put(_config("thread-b"), _checkpoint("0001", 2), {}, {"value": "0001"})
    loaded_a = saver.get_tuple(_config("thread-a"))
    loaded_b = saver.get_tuple(_config("thread-b"))
    assert loaded_a is not None and loaded_a.checkpoint["channel_values"] == {"value": 1}
    assert loaded_b is not None and loaded_b.checkpoint["channel_values"] == {"value": 2}
    with pytest.raises(PermissionError, match="project_scope_mismatch"):
        saver.get_tuple({"configurable": {"thread_id": "thread-a", "project": "other"}})


def test_bounds_reject_large_state_and_write(tmp_path: Path) -> None:
    saver = _saver(tmp_path, max_state_bytes=64, max_write_bytes=8)
    with pytest.raises(ValueError, match="state_too_large"):
        saver.put(_config(), _checkpoint("0001", "x" * 100), {}, {"value": "0001"})

    saver = _saver(tmp_path / "writes", max_state_bytes=4096, max_write_bytes=8)
    config = saver.put(_config(), _checkpoint("0001", 1), {}, {"value": "0001"})
    with pytest.raises(ValueError, match="write_too_large"):
        saver.put_writes(config, [("value", "x" * 100)], "node")


def test_prune_keeps_required_parent_chain_and_delete_is_safe(tmp_path: Path) -> None:
    saver = _saver(tmp_path)
    saver.put(_config(), _checkpoint("0001", 1), {}, {"value": "0001"})
    saver.put(_config(checkpoint_id="0001"), _checkpoint("0002", 2), {}, {"value": "0002"})
    saver.put(_config(checkpoint_id="0002"), _checkpoint("0003", 3), {}, {"value": "0003"})
    saver.prune(["thread-1"], strategy="keep_latest")
    assert len(list(saver.list(_config()))) == 3
    saver.prune(["thread-1"], strategy="delete")
    assert list(saver.list(_config())) == []


def test_concurrent_writers_and_copy_thread(tmp_path: Path) -> None:
    saver = _saver(tmp_path)

    def write(index: int) -> None:
        thread = f"thread-{index}"
        saver.put(_config(thread), _checkpoint("0001", index), {}, {"value": "0001"})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(8)))
    assert len(list(saver.list(None))) == 8

    saver.copy_thread("thread-0", "thread-copy")
    assert saver.get_tuple(_config("thread-copy")) is not None
    with pytest.raises(ValueError, match="target_not_empty"):
        saver.copy_thread("thread-0", "thread-copy")


def test_process_reopen_after_writer_exit(tmp_path: Path) -> None:
    database = tmp_path / "process.sqlite3"
    code = """
from pathlib import Path
import os
from blackholememory.langgraph_checkpoint import SQLiteLangGraphCheckpointSaver
from langgraph.checkpoint.base import empty_checkpoint
saver = SQLiteLangGraphCheckpointSaver(Path(__import__('sys').argv[1]), project='p', caller_id='c', task_id='t', session_id='s', enabled=True)
cp = empty_checkpoint()
cp.update(id='0001', channel_values={'value': 7}, channel_versions={'value': '0001'})
saver.put({'configurable': {'thread_id': 'thread'}}, cp, {'step': 1}, {'value': '0001'})
os._exit(0)
"""
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    completed = subprocess.run(
        [sys.executable, "-c", code, str(database)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    reopened = SQLiteLangGraphCheckpointSaver(
        database,
        project="p",
        caller_id="c",
        task_id="t",
        session_id="s",
        enabled=True,
    )
    assert reopened.get_tuple(_config("thread")) is not None


def test_langgraph_graph_uses_disposable_saver_and_async_read(tmp_path: Path) -> None:
    saver = _saver(tmp_path)
    builder = StateGraph(_State)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    graph = builder.compile(checkpointer=saver)

    assert graph.invoke({"value": 1}, {"configurable": {"thread_id": "graph-thread"}}) == {"value": 2}
    loaded = asyncio.run(saver.aget_tuple(_config("graph-thread")))
    assert loaded is not None
    assert loaded.checkpoint["channel_values"]["value"] == 2
