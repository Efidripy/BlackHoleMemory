from __future__ import annotations

from pathlib import Path

import blackholememory.hook_queue as hook_queue
from blackholememory.resource_limits import SQLITE_HOOK_QUEUE_BUSY_TIMEOUT_SECONDS


def test_hook_queue_busy_timeout_is_registry_backed() -> None:
    source = Path(hook_queue.__file__).read_text(encoding="utf-8")
    assert "SQLITE_HOOK_QUEUE_BUSY_TIMEOUT_SECONDS" in source
    assert "HOOK_QUEUE_BUSY_TIMEOUT_MS = 5_000" not in source
    assert SQLITE_HOOK_QUEUE_BUSY_TIMEOUT_SECONDS == 5.0
    assert hook_queue.HOOK_QUEUE_BUSY_TIMEOUT_MS == 5000

