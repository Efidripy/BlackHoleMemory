from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from blackholememory import app as bhm_app
from blackholememory import bhm_mcp


def test_task_open_creates_one_canonical_session_and_is_idempotent(monkeypatch):
    task_store: list[dict] = []
    session_calls: list[object] = []

    monkeypatch.setattr(bhm_app, "_load_tasks", lambda: list(task_store))

    def save_tasks(items: list[dict]) -> Path:
        task_store[:] = items
        return Path("tasks.json")

    monkeypatch.setattr(bhm_app, "_save_tasks", save_tasks)

    def create_session(request):
        session_calls.append(request)
        return (
            "created" if len(session_calls) == 1 else "updated",
            {"id": "session_bhm_task_001", "memory_id": "mem_bhm_task_001"},
        )

    monkeypatch.setattr(bhm_app, "_create_session_record", create_session)

    request = bhm_app.TaskOpenRequest(
        project="BlackHoleMemory",
        task_id="task-lifecycle-001",
        intent="Implement task lifecycle",
        scope_in=["src/blackholememory"],
        scope_out=["runtime/backups"],
        session_id="session-001",
        correlation_id="corr-001",
    )

    action, first = bhm_app._open_task(request)
    repeated_action, repeated = bhm_app._open_task(request)

    assert action == "created"
    assert repeated_action == "already_open"
    assert first == repeated
    assert len(task_store) == 1
    assert len(session_calls) == 1
    assert task_store[0]["project"] == "blackholememory"
    assert task_store[0]["session_record_id"] == "session_bhm_task_001"
    assert task_store[0]["memory_id"] == "mem_bhm_task_001"
    assert task_store[0]["metadata"]["session_upsert_key"] == (
        "session-record:task:blackholememory:task-lifecycle-001"
    )


def test_task_open_updates_existing_task_without_creating_a_second_task(monkeypatch):
    task_store: list[dict] = []
    session_calls: list[object] = []
    monkeypatch.setattr(bhm_app, "_load_tasks", lambda: list(task_store))
    monkeypatch.setattr(bhm_app, "_save_tasks", lambda items: task_store.__setitem__(slice(None), items) or Path("tasks.json"))
    monkeypatch.setattr(
        bhm_app,
        "_create_session_record",
        lambda request: (session_calls.append(request) or "updated", {"id": "session-1", "memory_id": "mem-1"}),
    )

    first_request = bhm_app.TaskOpenRequest(
        project="blackholememory",
        task_id="task-update-001",
        intent="Initial intent",
    )
    second_request = first_request.model_copy(update={"intent": "Refined intent"})

    first_action, _ = bhm_app._open_task(first_request)
    second_action, updated = bhm_app._open_task(second_request)

    assert first_action == "created"
    assert second_action == "updated"
    assert updated["intent"] == "Refined intent"
    assert len(task_store) == 1
    assert len(session_calls) == 2


def test_task_open_serializes_concurrent_first_open(monkeypatch):
    task_store: list[dict] = []
    session_calls: list[object] = []
    monkeypatch.setattr(bhm_app, "_load_tasks", lambda: list(task_store))
    monkeypatch.setattr(bhm_app, "_save_tasks", lambda items: task_store.__setitem__(slice(None), items) or Path("tasks.json"))
    monkeypatch.setattr(
        bhm_app,
        "_create_session_record",
        lambda request: (session_calls.append(request) or "created", {"id": "session-1", "memory_id": "mem-1"}),
    )
    request = bhm_app.TaskOpenRequest(
        project="blackholememory",
        task_id="task-concurrent-001",
        intent="Concurrent task open",
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(bhm_app._open_task, [request] * 4))

    assert [action for action, _ in results].count("created") == 1
    assert [action for action, _ in results].count("already_open") == 3
    assert len(task_store) == 1
    assert len(session_calls) == 1


def test_task_close_updates_one_canonical_session_and_is_idempotent(monkeypatch):
    task_store: list[dict] = []
    session_calls: list[object] = []
    monkeypatch.setattr(bhm_app, "_load_tasks", lambda: list(task_store))
    monkeypatch.setattr(
        bhm_app,
        "_save_tasks",
        lambda items: task_store.__setitem__(slice(None), items) or Path("tasks.json"),
    )

    def create_session(request):
        session_calls.append(request)
        return "updated", {"id": "session-1", "memory_id": "mem-1"}

    monkeypatch.setattr(bhm_app, "_create_session_record", create_session)
    open_request = bhm_app.TaskOpenRequest(
        project="blackholememory",
        task_id="task-close-001",
        intent="Implement task close",
        files_touched=["src/blackholememory/app.py"],
    )
    close_request = bhm_app.TaskCloseRequest(
        project="blackholememory",
        task_id="task-close-001",
        done="implemented and verified",
        next="move to P5.3",
        checks="pytest and smoke",
        risks="none",
        decisions="one canonical session",
        validation="green",
        conversation_notes="close once",
    )

    open_action, _ = bhm_app._open_task(open_request)
    close_action, closed = bhm_app._close_task(close_request)
    repeated_action, repeated = bhm_app._close_task(close_request)

    assert open_action == "created"
    assert close_action == "closed"
    assert repeated_action == "already_closed"
    assert repeated == closed
    assert closed["status"] == "closed"
    assert closed["done"] == "implemented and verified"
    assert closed["validation"] == "green"
    assert len(task_store) == 1
    assert len(session_calls) == 2


def test_task_close_reclose_updates_same_task_and_session(monkeypatch):
    task_store: list[dict] = []
    session_calls: list[object] = []
    monkeypatch.setattr(bhm_app, "_load_tasks", lambda: list(task_store))
    monkeypatch.setattr(
        bhm_app,
        "_save_tasks",
        lambda items: task_store.__setitem__(slice(None), items) or Path("tasks.json"),
    )
    monkeypatch.setattr(
        bhm_app,
        "_create_session_record",
        lambda request: (
            session_calls.append(request) or "updated",
            {"id": "session-1", "memory_id": "mem-1"},
        ),
    )
    open_request = bhm_app.TaskOpenRequest(
        project="blackholememory",
        task_id="task-reclose-001",
        intent="Implement task close",
    )
    first_close = bhm_app.TaskCloseRequest(
        project="blackholememory",
        task_id="task-reclose-001",
        done="first result",
    )
    second_close = first_close.model_copy(update={"done": "corrected result"})

    bhm_app._open_task(open_request)
    assert bhm_app._close_task(first_close)[0] == "closed"
    action, record = bhm_app._close_task(second_close)

    assert action == "reclosed"
    assert record["done"] == "corrected result"
    assert record["session_record_id"] == "session-1"
    assert len(task_store) == 1
    assert len(session_calls) == 3


def test_task_mcp_wrappers_use_public_task_routes(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        bhm_mcp,
        "_post",
        lambda path, body: calls.append((path, body)) or {"ok": True},
    )
    monkeypatch.setattr(
        bhm_mcp,
        "_get",
        lambda path, params: calls.append((path, params)) or {"ok": True},
    )

    assert bhm_mcp.bhm_task_open(
        "task-mcp-001",
        "Test public task contract",
        project="blackholememory",
        scope_in_csv="src,tests",
        metadata_json='{"priority":"high"}',
    ) == {"ok": True}
    assert bhm_mcp.bhm_task_close(
        "task-mcp-001",
        project="blackholememory",
        done="closed",
        next_step="continue",
        checks="green",
        validation="passed",
        files_touched_csv="src,tests",
        metadata_json='{"priority":"high"}',
    ) == {"ok": True}
    assert bhm_mcp.bhm_task_get("task-mcp-001", project="blackholememory") == {"ok": True}
    assert bhm_mcp.bhm_task_list(project="blackholememory", status="open", limit=5) == {"ok": True}

    assert calls[0] == (
        "/bhm/task/open",
        {
            "project": "blackholememory",
            "task_id": "task-mcp-001",
            "intent": "Test public task contract",
            "title": "",
            "scope_in": ["src", "tests"],
            "scope_out": [],
            "repo": "",
            "owner": "",
            "session_id": "",
            "correlation_id": "",
            "files_touched": [],
            "metadata": {"priority": "high"},
            "upsert_key": None,
        },
    )
    assert calls[1] == (
        "/bhm/task/close",
        {
            "project": "blackholememory",
            "task_id": "task-mcp-001",
            "done": "closed",
            "next": "continue",
            "checks": "green",
            "risks": "",
            "decisions": "",
            "validation": "passed",
            "files_touched": ["src", "tests"],
            "conversation_notes": "",
            "transcript_ref": "",
            "metadata": {"priority": "high"},
        },
    )
    assert calls[2] == ("/bhm/task", {"task_id": "task-mcp-001", "project": "blackholememory"})
    assert calls[3] == (
        "/bhm/tasks",
        {"limit": 5, "offset": 0, "project": "blackholememory", "status": "open"},
    )
