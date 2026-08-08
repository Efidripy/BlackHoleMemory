from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from blackholememory.code_graph_artifact import CodeGraphArtifactError
from blackholememory.code_graph_artifact import export_graph_artifact
from blackholememory.code_graph_artifact import verify_graph_artifact
from blackholememory.cross_repo_links import project_scope_is_aggregate
from blackholememory.memory_pulse_bus import MemoryPulseBus


class _Socket:
    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.payloads: list[dict] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.payloads.append(payload)


def test_cross_repo_aggregate_scope_is_explicit() -> None:
    assert project_scope_is_aggregate("*")
    assert project_scope_is_aggregate("blackholememory")
    assert not project_scope_is_aggregate("e-github-workspace")


def test_pulse_bus_bounds_clients_and_send_timeouts() -> None:
    async def scenario() -> None:
        bus = MemoryPulseBus(max_clients=1, send_timeout_seconds=0.01)
        first = _Socket(delay=0.05)
        second = _Socket()
        await bus.connect(first)
        with pytest.raises(RuntimeError, match="pulse_client_limit"):
            await bus.connect(second)
        await bus.broadcast({"event": "pulse", "node_id": "n", "project": "demo"})
        assert bus.client_count == 0

    asyncio.run(scenario())


def test_graph_artifact_verification_binds_project_and_root(tmp_path: Path) -> None:
    material = {
        "graph_digest": "a" * 64,
        "repository_snapshot_id": "repo-snapshot",
        "graph_snapshot_id": "graph-snapshot",
        "nodes": [{"node_id": "n1", "stable_key": "n1"}],
        "edges": [{"edge_id": "e1", "stable_key": "e1"}],
        "parse_results": [],
    }
    exported = export_graph_artifact(material, runtime_dir=tmp_path, project="demo", root_id="root-demo")
    with pytest.raises(CodeGraphArtifactError, match="project"):
        verify_graph_artifact(
            exported["path"],
            runtime_dir=tmp_path,
            expected_project="other",
            expected_root_id="root-demo",
        )
    verified = verify_graph_artifact(
        exported["path"],
        runtime_dir=tmp_path,
        expected_project="demo",
        expected_root_id="root-demo",
    )
    assert verified["artifact"]["project"] == "demo"
