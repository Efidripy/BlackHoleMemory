"""Regression coverage for sync I/O admission on async REST handlers."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from blackholememory import app as bhm_app
from blackholememory.memory_repository import SQLiteMemoryRepository


class _TrackedConnection:
    """Small proxy that makes connection closure observable in tests."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.closed = False

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.closed = True
        self.connection.close()


def test_advanced_search_does_not_freeze_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ready() -> None:
        return None

    def blocking_search(request: bhm_app.MemoryAdvancedSearchRequest) -> tuple[list[dict], int]:
        del request
        time.sleep(0.15)
        return [], 0

    monkeypatch.setattr(bhm_app, "_ensure_provider_warmup_ready", ready)
    monkeypatch.setattr(bhm_app, "_advanced_search_live_memories", blocking_search)

    async def exercise() -> tuple[float, int]:
        ticks: list[float] = []
        started = time.perf_counter()

        async def ticker() -> None:
            while True:
                ticks.append(time.perf_counter() - started)
                await asyncio.sleep(0.005)

        ticker_task = asyncio.create_task(ticker())
        try:
            response = await bhm_app.bhm_search_advanced(
                bhm_app.MemoryAdvancedSearchRequest(
                    query="wl010",
                    project="blackholememory",
                    limit=5,
                    offset=0,
                )
            )
            assert response["total"] == 0
        finally:
            ticker_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ticker_task

        gaps = [later - earlier for earlier, later in zip(ticks, ticks[1:])]
        return max(gaps, default=0.0), len(ticks)

    max_gap, tick_count = asyncio.run(exercise())
    assert tick_count >= 10
    assert max_gap < 0.08
    assert bhm_app._READ_BACKPRESSURE_ACTIVE == 0
    assert bhm_app._READ_BACKPRESSURE_WAITING == 0


def test_bounded_read_propagates_sqlite_error_and_releases_admission() -> None:
    def failing_read() -> None:
        raise sqlite3.OperationalError("database is locked")

    async def exercise() -> None:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await bhm_app._run_bounded_read("test.sqlite", failing_read)

    asyncio.run(exercise())
    assert bhm_app._READ_BACKPRESSURE_ACTIVE == 0
    assert bhm_app._READ_BACKPRESSURE_WAITING == 0


def test_code_tools_status_does_not_run_index_reads_on_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bhm_app,
        "_public_code_request_scope",
        lambda request: (Path("repo"), "demo"),
    )
    monkeypatch.setattr(bhm_app, "_public_code_root_id", lambda *args, **kwargs: "root-id")

    def blocking_index_status(*args, **kwargs) -> dict:
        del args, kwargs
        time.sleep(0.15)
        return {"status": "ready"}

    monkeypatch.setattr(bhm_app, "repository_index_status", blocking_index_status)
    monkeypatch.setattr(bhm_app, "_current_code_graph_snapshot", lambda *args, **kwargs: {})

    async def exercise() -> tuple[float, int]:
        ticks: list[float] = []
        started = time.perf_counter()

        async def ticker() -> None:
            while True:
                ticks.append(time.perf_counter() - started)
                await asyncio.sleep(0.005)

        ticker_task = asyncio.create_task(ticker())
        try:
            response = await bhm_app.bhm_public_code_tools(
                bhm_app.PublicCodeToolRequest(
                    operation="status",
                    project="demo",
                    root="repo",
                )
            )
            assert response["operation"] == "status"
        finally:
            ticker_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ticker_task

        gaps = [later - earlier for earlier, later in zip(ticks, ticks[1:])]
        return max(gaps, default=0.0), len(ticks)

    max_gap, tick_count = asyncio.run(exercise())
    assert tick_count >= 10
    assert max_gap < 0.08
    assert bhm_app._READ_BACKPRESSURE_ACTIVE == 0
    assert bhm_app._READ_BACKPRESSURE_WAITING == 0


@pytest.mark.parametrize("admission", ["read", "write"])
def test_bounded_admission_cancellation_releases_waiter_and_slot(
    monkeypatch: pytest.MonkeyPatch,
    admission: str,
) -> None:
    async def exercise() -> None:
        prefix = admission.upper()
        semaphore = asyncio.Semaphore(1)
        lock = asyncio.Lock()
        monkeypatch.setattr(bhm_app, f"_{prefix}_SEMAPHORE", semaphore)
        monkeypatch.setattr(bhm_app, f"_{prefix}_BACKPRESSURE_LOCK", lock)
        monkeypatch.setattr(bhm_app, f"_{prefix}_BACKPRESSURE_ACTIVE", 0)
        monkeypatch.setattr(bhm_app, f"_{prefix}_BACKPRESSURE_WAITING", 0)
        monkeypatch.setattr(bhm_app, f"_{prefix}_ACQUIRE_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setattr(bhm_app, f"_{prefix}_QUEUE_LIMIT", 1)

        admission_context = getattr(bhm_app, f"_bounded_{admission}")
        runner = getattr(bhm_app, f"_run_bounded_{admission}")
        started = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with admission_context("holder"):
                started.set()
                await release.wait()

        holder_task = asyncio.create_task(holder())
        await started.wait()

        waiter_task = asyncio.create_task(runner("cancelled", lambda: None))
        for _ in range(100):
            if getattr(bhm_app, f"_{prefix}_BACKPRESSURE_WAITING") == 1:
                break
            await asyncio.sleep(0)
        assert getattr(bhm_app, f"_{prefix}_BACKPRESSURE_WAITING") == 1

        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task

        assert getattr(bhm_app, f"_{prefix}_BACKPRESSURE_ACTIVE") == 1
        assert getattr(bhm_app, f"_{prefix}_BACKPRESSURE_WAITING") == 0

        release.set()
        await holder_task
        assert getattr(bhm_app, f"_{prefix}_BACKPRESSURE_ACTIVE") == 0
        assert getattr(semaphore, "_value") == 1

    asyncio.run(exercise())


def test_cancelled_bounded_read_does_not_skip_sync_resource_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        monkeypatch.setattr(bhm_app, "_READ_SEMAPHORE", asyncio.Semaphore(1))
        monkeypatch.setattr(bhm_app, "_READ_BACKPRESSURE_LOCK", asyncio.Lock())
        monkeypatch.setattr(bhm_app, "_READ_BACKPRESSURE_ACTIVE", 0)
        monkeypatch.setattr(bhm_app, "_READ_BACKPRESSURE_WAITING", 0)
        monkeypatch.setattr(bhm_app, "_READ_ACQUIRE_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setattr(bhm_app, "_READ_QUEUE_LIMIT", 1)

        opened = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        connections: list[sqlite3.Connection] = []

        def blocking_read() -> None:
            connection = sqlite3.connect(":memory:", check_same_thread=False)
            connections.append(connection)
            opened.set()
            try:
                while not release.is_set():
                    time.sleep(0.005)
            finally:
                connection.close()
                closed.set()

        task = asyncio.create_task(bhm_app._run_bounded_read("cancelled-resource", blocking_read))
        assert await asyncio.to_thread(opened.wait, 1.0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert bhm_app._READ_BACKPRESSURE_ACTIVE == 0
        assert bhm_app._READ_BACKPRESSURE_WAITING == 0

        release.set()
        assert await asyncio.to_thread(closed.wait, 1.0)
        with pytest.raises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")

    asyncio.run(exercise())


def test_bounded_read_and_write_close_sqlite_connections_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = SQLiteMemoryRepository(tmp_path / "memories.sqlite3")
    repository.initialize()
    read_connection = _TrackedConnection(
        sqlite3.connect(repository.path, check_same_thread=False)
    )
    read_connection.connection.row_factory = sqlite3.Row
    monkeypatch.setattr(repository, "_read_connection", lambda: read_connection)

    write_connection = _TrackedConnection(
        sqlite3.connect(repository.path, check_same_thread=False, isolation_level=None)
    )
    write_connection.connection.execute("PRAGMA foreign_keys=ON")
    monkeypatch.setattr(repository, "_connect", lambda: write_connection)

    def read() -> list:
        return repository.list_memories(
            include_archived=True,
            include_tombstoned=True,
        )

    def write() -> None:
        with repository._write_transaction() as connection:
            connection.execute("SELECT 1")

    async def exercise() -> None:
        await bhm_app._run_bounded_read("sqlite.read", read)
        await bhm_app._run_bounded_write("sqlite.write", write)

    asyncio.run(exercise())
    assert read_connection.closed is True
    assert write_connection.closed is True
    with pytest.raises(sqlite3.ProgrammingError):
        read_connection.connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        write_connection.connection.execute("SELECT 1")


def test_mixed_bounded_read_write_smoke_respects_admission_limits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def exercise() -> tuple[int, int, list[object]]:
        monkeypatch.setattr(bhm_app, "_READ_SEMAPHORE", asyncio.Semaphore(2))
        monkeypatch.setattr(bhm_app, "_READ_BACKPRESSURE_LOCK", asyncio.Lock())
        monkeypatch.setattr(bhm_app, "_READ_BACKPRESSURE_ACTIVE", 0)
        monkeypatch.setattr(bhm_app, "_READ_BACKPRESSURE_WAITING", 0)
        monkeypatch.setattr(bhm_app, "_READ_QUEUE_LIMIT", 8)
        monkeypatch.setattr(bhm_app, "_READ_ACQUIRE_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setattr(bhm_app, "_WRITE_SEMAPHORE", asyncio.Semaphore(1))
        monkeypatch.setattr(bhm_app, "_WRITE_BACKPRESSURE_LOCK", asyncio.Lock())
        monkeypatch.setattr(bhm_app, "_WRITE_BACKPRESSURE_ACTIVE", 0)
        monkeypatch.setattr(bhm_app, "_WRITE_BACKPRESSURE_WAITING", 0)
        monkeypatch.setattr(bhm_app, "_WRITE_QUEUE_LIMIT", 8)
        monkeypatch.setattr(bhm_app, "_WRITE_ACQUIRE_TIMEOUT_SECONDS", 1.0)

        repository = SQLiteMemoryRepository(tmp_path / "mixed.sqlite3")
        repository.initialize()
        active_read = 0
        active_write = 0
        max_read = 0
        max_write = 0
        counters_lock = threading.Lock()

        def read_work() -> int:
            nonlocal active_read, max_read
            with counters_lock:
                active_read += 1
                max_read = max(max_read, active_read)
            try:
                result = len(repository.list_memories(include_archived=True, include_tombstoned=True))
                time.sleep(0.02)
                return result
            finally:
                with counters_lock:
                    active_read -= 1

        def write_work() -> None:
            nonlocal active_write, max_write
            with counters_lock:
                active_write += 1
                max_write = max(max_write, active_write)
            try:
                with repository._write_transaction() as connection:
                    connection.execute("SELECT 1")
                    time.sleep(0.02)
            finally:
                with counters_lock:
                    active_write -= 1

        tasks = [
            asyncio.create_task(bhm_app._run_bounded_read(f"mixed.read.{index}", read_work))
            for index in range(6)
        ]
        tasks.extend(
            asyncio.create_task(bhm_app._run_bounded_write(f"mixed.write.{index}", write_work))
            for index in range(4)
        )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return max_read, max_write, results

    max_read, max_write, results = asyncio.run(exercise())
    assert all(not isinstance(result, Exception) for result in results)
    assert 1 <= max_read <= 2
    assert max_write == 1
    assert bhm_app._READ_BACKPRESSURE_ACTIVE == 0
    assert bhm_app._READ_BACKPRESSURE_WAITING == 0
    assert bhm_app._WRITE_BACKPRESSURE_ACTIVE == 0
    assert bhm_app._WRITE_BACKPRESSURE_WAITING == 0
