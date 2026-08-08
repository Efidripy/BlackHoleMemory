from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from blackholememory import app as bhm_app
from blackholememory.hook_queue import HookQueueError
from blackholememory.mem0_adapter import StorageNotReady


_SECRET_MESSAGE = (
    "Authorization: Bearer synthetic-bearer-token; "
    "password=synthetic-password; "
    "path=C:\\runtime\\secret-token\\memories.sqlite"
)


def test_safe_exception_text_is_bounded_and_redacts_secrets() -> None:
    safe = bhm_app._safe_exception_text(RuntimeError(_SECRET_MESSAGE), limit=48)

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

    monkeypatch.setattr(bhm_app, "_HOOK_QUEUE_ACCEPTING", True)
    monkeypatch.setattr(bhm_app.asyncio, "to_thread", failing_to_thread)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(bhm_app._enqueue_hook_request("compact", request))

    detail = raised.value.detail
    assert detail["error"] == "hook_queue_unavailable"
    assert "synthetic-bearer-token" not in detail["detail"]
    assert "synthetic-password" not in detail["detail"]
    assert "secret-token" not in detail["detail"]
