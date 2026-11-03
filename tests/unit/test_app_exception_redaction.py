from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from blackholememory import app as bhm_app
from blackholememory import app_hooks, app_runtime
from blackholememory.hook_queue import HookQueueError
from blackholememory.mem0_adapter import StorageNotReady

_SECRET_MESSAGE = (
    "Authorization: Bearer synthetic-bearer-token; "
    "password=synthetic-password; "
    "path=C:\\runtime\\secret-token\\memories.sqlite"
)


def test_safe_exception_text_is_bounded_and_redacts_secrets() -> None:
    safe = app_runtime._safe_exception_text(RuntimeError(_SECRET_MESSAGE), limit=48)

    assert len(safe) <= 48
    assert "synthetic-bearer-token" not in safe
    assert "synthetic-password" not in safe
    assert "secret-token" not in safe
    assert "[REDACTED:" in safe


def test_storage_not_ready_handler_does_not_return_raw_exception_text() -> None:
    response = asyncio.run(
        bhm_app.storage_not_ready_handler(
            SimpleNamespace(),
            StorageNotReady(_SECRET_MESSAGE),
        )
    )
    payload = json.loads(response.body)

    reason = payload["detail"]["reason"]
    assert "synthetic-bearer-token" not in reason
    assert "synthetic-password" not in reason
    assert "secret-token" not in reason


def test_hook_queue_rest_boundary_redacts_queue_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    request = bhm_app.BhmHookCompactRequest(
        hookType="codex_post_tool_use",
        sessionId="session-redaction",
        project="blackholememory",
    )

    async def failing_to_thread(*_args, **_kwargs):
        raise HookQueueError(_SECRET_MESSAGE)

    monkeypatch.setattr(app_hooks, "_HOOK_QUEUE_ACCEPTING", True)
    monkeypatch.setattr(bhm_app.asyncio, "to_thread", failing_to_thread)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(app_hooks._enqueue_hook_request("compact", request))

    detail = raised.value.detail
    assert detail["error"] == "hook_queue_unavailable"
    assert "synthetic-bearer-token" not in detail["detail"]
    assert "synthetic-password" not in detail["detail"]
    assert "secret-token" not in detail["detail"]


def test_bhm_search_unhandled_exception_logs_traceback_and_returns_500_without_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from blackholememory import app_retrieval
    from blackholememory.memory_search_service import MemorySearchService

    async def fail_execute(*_args, **_kwargs):
        raise RuntimeError("database exploded with password=synthetic-secret")

    monkeypatch.setattr(MemorySearchService, "execute", fail_execute)

    request = bhm_app.MemoryAdvancedSearchRequest(
        query="super secret query text",
        project="alpha-project",
    )

    with caplog.at_level(logging.ERROR, logger="blackholememory.app_retrieval"):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(app_retrieval.bhm_search(request))

    assert raised.value.status_code == 500
    assert raised.value.detail == "Internal server error"
    assert "super secret query text" not in str(raised.value.detail)
    assert "synthetic-secret" not in str(raised.value.detail)

    records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(records) == 1
    assert "RuntimeError" in records[0].message
    assert "alpha-project" in records[0].message
    assert "super secret query text" not in records[0].message
    assert records[0].exc_info is not None


def test_bhm_search_http_exception_is_reraised_without_modification(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from blackholememory import app_retrieval
    from blackholememory.memory_search_service import MemorySearchService

    async def fail_http(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="forbidden_scope")

    monkeypatch.setattr(MemorySearchService, "execute", fail_http)

    request = bhm_app.MemoryAdvancedSearchRequest(
        query="test query",
        project="alpha-project",
    )

    with caplog.at_level(logging.ERROR, logger="blackholememory.app_retrieval"):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(app_retrieval.bhm_search(request))

    assert raised.value.status_code == 403
    assert raised.value.detail == "forbidden_scope"
    assert not caplog.records


def test_bhm_search_success_returns_result_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blackholememory import app_retrieval
    from blackholememory.memory_search_service import MemorySearchService

    expected = {"memories": [{"id": "m1"}], "total": 1}

    async def succeed(*_args, **_kwargs):
        return expected

    monkeypatch.setattr(MemorySearchService, "execute", succeed)

    request = bhm_app.MemoryAdvancedSearchRequest(
        query="test query",
        project="alpha-project",
    )

    result = asyncio.run(app_retrieval.bhm_search(request))
    assert result == expected
