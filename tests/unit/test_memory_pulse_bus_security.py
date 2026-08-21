from __future__ import annotations

import asyncio
import logging

from blackholememory.app import MemoryPulseBus


class _FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.payloads: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.payloads.append(payload)


class _FailingWebSocket(_FakeWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.send_attempted = asyncio.Event()

    async def send_json(self, payload: dict) -> None:
        self.send_attempted.set()
        raise ValueError("synthetic socket serialization failure")


def test_pulse_bus_filters_project_events_but_keeps_system_events_global() -> None:
    async def scenario() -> None:
        bus = MemoryPulseBus()
        all_projects = _FakeWebSocket()
        blackholememory = _FakeWebSocket()
        workspace = _FakeWebSocket()
        await bus.connect(all_projects, None)
        await bus.connect(blackholememory, frozenset({"blackholememory"}))
        await bus.connect(workspace, frozenset({"e-github-workspace"}))

        await bus.broadcast({"event": "pulse", "node_id": "one", "project": "BlackHoleMemory"})
        await bus.broadcast({"event": "pulse", "node_id": "unknown", "project": ""})
        await bus.broadcast({"event": "sys_status", "data": {"ok": True}})

        assert [item["event"] for item in all_projects.payloads] == ["pulse", "pulse", "sys_status"]
        assert [item["event"] for item in blackholememory.payloads] == ["pulse", "sys_status"]
        assert [item["event"] for item in workspace.payloads] == ["sys_status"]

    asyncio.run(scenario())


def test_emit_pulse_absorbs_generic_send_failure_and_restores_counters(caplog) -> None:
    async def scenario() -> list[dict]:
        bus = MemoryPulseBus()
        failing = _FailingWebSocket()
        await bus.connect(failing)
        loop = asyncio.get_running_loop()
        observed_task_errors: list[dict] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: observed_task_errors.append(context))
        try:
            bus.emit_pulse("node-1", "demo")
            await asyncio.wait_for(failing.send_attempted.wait(), timeout=1)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)
        assert bus.client_count == 0
        assert bus.pending_broadcast_count == 0
        return observed_task_errors

    with caplog.at_level(logging.WARNING, logger="blackholememory.memory_pulse_bus"):
        observed_task_errors = asyncio.run(scenario())

    assert observed_task_errors == []
    records = [record for record in caplog.records if record.message == "memory_pulse_client_send_failed"]
    assert len(records) == 1
    assert records[0].error_type == "ValueError"
