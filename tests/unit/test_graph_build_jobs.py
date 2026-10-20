from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.graph_build_jobs import GRAPH_BUILD_INTERRUPTED_ERROR
from blackholememory.graph_build_jobs import GraphBuildBusy
from blackholememory.graph_build_jobs import SQLiteGraphBuildJobStore


def _start(store: SQLiteGraphBuildJobStore, operation_id: str, snapshot_id: str, boot_id: str = "boot-a"):
    return store.start(
        operation_id=operation_id,
        project="demo",
        root_id="root-1",
        root="E:/repos/demo",
        snapshot_id=snapshot_id,
        boot_id=boot_id,
    )


def test_graph_build_job_dedupes_and_allows_one_active_build_per_root(tmp_path: Path) -> None:
    store = SQLiteGraphBuildJobStore(tmp_path / "db.sqlite3")
    record, started = _start(store, "op-1", "snap-1")
    assert started and record["status"] == "running" and record["phase"] == "queued"

    again, started_again = _start(store, "op-1", "snap-1")
    assert not started_again and again["operation_id"] == "op-1"

    with pytest.raises(GraphBuildBusy) as busy:
        _start(store, "op-2", "snap-2")
    assert busy.value.active["operation_id"] == "op-1"

    store.update("op-1", phase="parsing", files_parsed=3, files_total=10)
    progressed = store.get("op-1")
    assert progressed["files_parsed"] == 3 and progressed["updated_at"] >= record["updated_at"]

    store.update("op-1", status="completed", phase="completed", result={"graph_snapshot_id": "g1"}, retryable=False)
    assert store.latest("demo", "root-1")["result"] == {"graph_snapshot_id": "g1"}
    assert _start(store, "op-1", "snap-1")[1] is False
    assert _start(store, "op-2", "snap-2")[1] is True


def test_graph_build_job_left_running_by_previous_boot_is_retryable_and_restarts(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    _start(SQLiteGraphBuildJobStore(database), "op-1", "snap-1", boot_id="old-boot")

    restarted_store = SQLiteGraphBuildJobStore(database)
    assert restarted_store.recover_interrupted("new-boot") == 1
    interrupted = restarted_store.get("op-1")
    assert interrupted["status"] == "failed"
    assert interrupted["error"] == GRAPH_BUILD_INTERRUPTED_ERROR
    assert interrupted["retryable"] is True

    record, started = _start(restarted_store, "op-1", "snap-1", boot_id="new-boot")
    assert started and record["status"] == "running" and record["error"] is None
