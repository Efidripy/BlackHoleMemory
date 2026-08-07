from __future__ import annotations

from blackholememory.agents.developer_agent import ChronicleLogger
from blackholememory.resource_limits import RUNTIME_CHRONICLE_APPEND_TIMEOUT_SECONDS


def test_chronicle_append_wait_uses_registry_timeout() -> None:
    captured: dict[str, object] = {}

    class Future:
        def result(self, *, timeout):
            captured["timeout"] = timeout

    class Executor:
        def submit(self, *_args):
            return Future()

    logger = ChronicleLogger.__new__(ChronicleLogger)
    logger._append_executor = Executor()
    logger._append_text = lambda _content: None

    logger.log_phase("TEST", "bounded")

    assert captured["timeout"] == RUNTIME_CHRONICLE_APPEND_TIMEOUT_SECONDS
