from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blackholememory import bhm_mcp_base
from blackholememory import app_ops_task
from blackholememory import app_ops_task2
from blackholememory import app as bhm_app
from blackholememory import app_runtime
from blackholememory import bhm_mcp


@pytest.fixture(autouse=True)
def reset_task_ingress_gate(monkeypatch):
    monkeypatch.setattr(app_ops_task2, "_TASK_INGRESS_FROZEN", False)
    monkeypatch.setattr(app_ops_task2, "_TASK_INGRESS_REASON", "")
    monkeypatch.setattr(app_ops_task2, "_TASK_INGRESS_CHANGED_AT", None)


def test_task_open_creates_one_canonical_session_and_is_idempotent(monkeypatch):
    task_store: list[dict] = []
    session_calls: list[object] = []

    monkeypatch.setattr(app_runtime, "_load_tasks", lambda: list(task_store))

    def save_tasks(items: list[dict]) -> Path:
        task_store[:] = items
        return Path("tasks.json")

    monkeypatch.setattr(app_runtime, "_save_tasks", save_tasks)

    def create_session(request):
        session_calls.append(request)
        return (
            "created" if len(session_calls) == 1 else "updated",
            {"id": "session_bhm_task_001", "memory_id": "mem_bhm_task_001"},
        )

    monkeypatch.setattr(app_ops_task2, "_create_session_record", create_session)

    request = bhm_app.TaskOpenRequest(
        project="BlackHoleMemory",
        task_id="task-lifecycle-001",
        intent="Implement task lifecycle",
        scope_in=["src/blackholememory"],
        scope_out=["runtime/backups"],
        session_id="session-001",
        correlation_id="corr-001",
    )

    action, first = app_ops_task2._open_task(request)
    repeated_action, repeated = app_ops_task2._open_task(request)

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
    monkeypatch.setattr(app_runtime, "_load_tasks", lambda: list(task_store))
    monkeypatch.setattr(app_runtime, "_save_tasks", lambda items: task_store.__setitem__(slice(None), items) or Path("tasks.json"))
    monkeypatch.setattr(
        app_ops_task2,
        "_create_session_record",
        lambda request: (session_calls.append(request) or "updated", {"id": "session-1", "memory_id": "mem-1"}),
    )

    first_request = bhm_app.TaskOpenRequest(
        project="blackholememory",
        task_id="task-update-001",
        intent="Initial intent",
    )
    second_request = first_request.model_copy(update={"intent": "Refined intent"})

    first_action, _ = app_ops_task2._open_task(first_request)
    second_action, updated = app_ops_task2._open_task(second_request)

    assert first_action == "created"
    assert second_action == "updated"
    assert updated["intent"] == "Refined intent"
    assert len(task_store) == 1
    assert len(session_calls) == 2


def test_task_open_serializes_concurrent_first_open(monkeypatch):
    task_store: list[dict] = []
    session_calls: list[object] = []
    monkeypatch.setattr(app_runtime, "_load_tasks", lambda: list(task_store))
    monkeypatch.setattr(app_runtime, "_save_tasks", lambda items: task_store.__setitem__(slice(None), items) or Path("tasks.json"))
    monkeypatch.setattr(
        app_ops_task2,
        "_create_session_record",
        lambda request: (session_calls.append(request) or "created", {"id": "session-1", "memory_id": "mem-1"}),
    )
    request = bhm_app.TaskOpenRequest(
        project="blackholememory",
        task_id="task-concurrent-001",
        intent="Concurrent task open",
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(app_ops_task2._open_task, [request] * 4))

    assert [action for action, _ in results].count("created") == 1
    assert [action for action, _ in results].count("already_open") == 3
    assert len(task_store) == 1
    assert len(session_calls) == 1


def test_task_ingress_freeze_blocks_open_and_close_without_lifecycle_writes(monkeypatch):
    task_store: list[dict] = []
    session_calls: list[object] = []
    task_writes: list[object] = []
    monkeypatch.setattr(app_runtime, "_load_tasks", lambda: list(task_store))
    monkeypatch.setattr(
        app_runtime,
        "_save_tasks",
        lambda items: task_writes.append(items) or task_store.__setitem__(slice(None), items) or Path("tasks.json"),
    )
    monkeypatch.setattr(
        app_ops_task2,
        "_create_session_record",
        lambda request: (session_calls.append(request) or "created", {"id": "session-1", "memory_id": "mem-1"}),
    )
    open_request = bhm_app.TaskOpenRequest(
        project="blackholememory",
        task_id="task-freeze-001",
        intent="Freeze task ingress",
    )
    close_request = bhm_app.TaskCloseRequest(project="blackholememory", task_id="task-freeze-001")

    assert app_ops_task2._open_task(open_request)[0] == "created"
    writes_before_freeze = len(task_writes)
    sessions_before_freeze = len(session_calls)
    action, status = app_ops_task2._freeze_task_ingress(
        bhm_app.TaskIngressControlRequest(reason="exact snapshot reconciliation")
    )

    assert action == "frozen"
    assert status["frozen"] is True
    assert status["reason"] == "exact snapshot reconciliation"
    for request, operation in ((open_request, app_ops_task2._open_task), (close_request, app_ops_task2._close_task)):
        with pytest.raises(bhm_app.HTTPException) as denied:
            operation(request)
        assert denied.value.status_code == 503
        assert denied.value.detail == {"code": "task_ingress_frozen"}
    assert len(task_writes) == writes_before_freeze
    assert len(session_calls) == sessions_before_freeze

    assert app_ops_task2._resume_task_ingress(
        bhm_app.TaskIngressControlRequest(reason="snapshot complete")
    )[0] == "opened"
    assert app_ops_task2._close_task(close_request)[0] == "closed"


def test_task_ingress_freeze_and_resume_are_idempotent():
    request = bhm_app.TaskIngressControlRequest(reason="planned maintenance")
    assert app_ops_task2._freeze_task_ingress(request)[0] == "frozen"
    assert app_ops_task2._freeze_task_ingress(request)[0] == "already_frozen"
    assert app_ops_task2._resume_task_ingress(request)[0] == "opened"
    action, status = app_ops_task2._resume_task_ingress(request)
    assert action == "already_open"
    assert status["frozen"] is False
    assert status["reason"] == ""


def test_task_ingress_reason_is_redacted_before_it_is_retained_or_returned():
    raw_reason = (
        "Bearer unit-test-secret-value-123456 "
        "path=C:\\Users\\operator\\secret-token-data "
        "token=unit-test-token-value-123456"
    )
    action, status = app_ops_task2._freeze_task_ingress(
        bhm_app.TaskIngressControlRequest(reason=raw_reason)
    )

    assert action == "frozen"
    assert "[REDACTED:authorization]" in status["reason"]
    assert "[REDACTED:path]" in status["reason"]
    assert "unit-test-secret" not in app_ops_task2._TASK_INGRESS_REASON
    assert "unit-test-token" not in app_ops_task2._TASK_INGRESS_REASON
    assert "C:\\Users\\operator" not in app_ops_task2._TASK_INGRESS_REASON
    assert "unit-test-secret" not in str(app_ops_task2._task_ingress_status())


def test_task_ingress_admin_routes_require_capability_and_status_is_read_only(monkeypatch):
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "unit-admin-capability")
    client = TestClient(bhm_app.app)
    denied = client.post(
        "/bhm/admin/task-ingress/freeze",
        json={"reason": "exact snapshot"},
    )
    invalid = client.get(
        "/bhm/admin/task-ingress/status",
        headers={"X-BHM-Admin-Capability": "wrong"},
    )
    headers = {"X-BHM-Admin-Capability": "unit-admin-capability"}
    status_before = client.get("/bhm/admin/task-ingress/status", headers=headers)
    frozen = client.post(
        "/bhm/admin/task-ingress/freeze",
        headers=headers,
        json={"reason": "exact snapshot"},
    )
    repeated = client.post(
        "/bhm/admin/task-ingress/freeze",
        headers=headers,
        json={"reason": "ignored replacement"},
    )
    resumed = client.post(
        "/bhm/admin/task-ingress/resume",
        headers=headers,
        json={"reason": "reconciliation complete"},
    )

    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "admin_capability_required"
    assert invalid.status_code == 403
    assert invalid.json()["detail"]["code"] == "admin_capability_required"
    assert status_before.status_code == 200
    assert status_before.json()["task_ingress"] == {
        "frozen": False,
        "reason": "",
        "changed_at": None,
    }
    assert frozen.status_code == 200
    assert frozen.json()["action"] == "frozen"
    assert frozen.json()["task_ingress"]["reason"] == "exact snapshot"
    assert repeated.status_code == 200
    assert repeated.json()["action"] == "already_frozen"
    assert repeated.json()["task_ingress"]["reason"] == "exact snapshot"
    assert resumed.status_code == 200
    assert resumed.json()["action"] == "opened"
    assert resumed.json()["task_ingress"]["frozen"] is False
    assert "capability" not in str(resumed.json()).casefold()


def test_task_ingress_freeze_rejects_public_task_routes_before_any_task_write(monkeypatch):
    monkeypatch.setenv("BHM_ADMIN_CAPABILITY", "unit-admin-capability")
    task_writes: list[object] = []
    session_calls: list[object] = []
    monkeypatch.setattr(app_runtime, "_load_tasks", lambda: [])
    monkeypatch.setattr(app_runtime, "_save_tasks", lambda items: task_writes.append(items))
    monkeypatch.setattr(
        app_ops_task2,
        "_create_session_record",
        lambda request: (session_calls.append(request) or "created", {"id": "session-1", "memory_id": "mem-1"}),
    )
    client = TestClient(bhm_app.app)
    admin_headers = {"X-BHM-Admin-Capability": "unit-admin-capability"}
    assert client.post(
        "/bhm/admin/task-ingress/freeze",
        headers=admin_headers,
        json={"reason": "exact snapshot"},
    ).status_code == 200

    opened = client.post(
        "/bhm/task/open",
        json={"project": "blackholememory", "task_id": "task-freeze-route-001", "intent": "blocked"},
    )
    closed = client.post(
        "/bhm/task/close",
        json={"project": "blackholememory", "task_id": "task-freeze-route-001"},
    )

    assert opened.status_code == 503
    assert opened.json()["detail"] == {"code": "task_ingress_frozen"}
    assert closed.status_code == 503
    assert closed.json()["detail"] == {"code": "task_ingress_frozen"}
    assert task_writes == []
    assert session_calls == []


def test_task_ingress_freeze_does_not_intercept_session_record_writes(monkeypatch):
    calls: list[str] = []

    async def bounded_write(operation, _func, *_args, **_kwargs):
        calls.append(operation)
        return "created", {"id": "session-1", "memory_id": "mem-1"}

    monkeypatch.setattr(app_runtime, "_run_bounded_write", bounded_write)
    assert app_ops_task2._freeze_task_ingress(
        bhm_app.TaskIngressControlRequest(reason="exact snapshot")
    )[0] == "frozen"

    response = asyncio.run(
        app_ops_task.bhm_session_record_create(
            bhm_app.SessionRecordCreateRequest(project="blackholememory", title="unaffected")
        )
    )

    assert response["success"] is True
    assert calls == ["bhm.session-record"]


def test_task_close_updates_one_canonical_session_and_is_idempotent(monkeypatch):
    task_store: list[dict] = []
    session_calls: list[object] = []
    monkeypatch.setattr(app_runtime, "_load_tasks", lambda: list(task_store))
    monkeypatch.setattr(
        app_runtime,
        "_save_tasks",
        lambda items: task_store.__setitem__(slice(None), items) or Path("tasks.json"),
    )

    def create_session(request):
        session_calls.append(request)
        return "updated", {"id": "session-1", "memory_id": "mem-1"}

    monkeypatch.setattr(app_ops_task2, "_create_session_record", create_session)
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

    open_action, _ = app_ops_task2._open_task(open_request)
    close_action, closed = app_ops_task2._close_task(close_request)
    repeated_action, repeated = app_ops_task2._close_task(close_request)

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
    monkeypatch.setattr(app_runtime, "_load_tasks", lambda: list(task_store))
    monkeypatch.setattr(
        app_runtime,
        "_save_tasks",
        lambda items: task_store.__setitem__(slice(None), items) or Path("tasks.json"),
    )
    monkeypatch.setattr(
        app_ops_task2,
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

    app_ops_task2._open_task(open_request)
    assert app_ops_task2._close_task(first_close)[0] == "closed"
    action, record = app_ops_task2._close_task(second_close)

    assert action == "reclosed"
    assert record["done"] == "corrected result"
    assert record["session_record_id"] == "session-1"
    assert len(task_store) == 1
    assert len(session_calls) == 3


def test_task_mcp_wrappers_use_public_task_routes(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        bhm_mcp_base,
        "_post",
        lambda path, body: calls.append((path, body)) or {"ok": True},
    )
    monkeypatch.setattr(
        bhm_mcp_base,
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
