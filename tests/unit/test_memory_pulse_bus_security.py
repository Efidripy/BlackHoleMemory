from __future__ import annotations

import asyncio

from blackholememory.app import MemoryPulseBus


class _FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.payloads: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.payloads.append(payload)


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
