from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-bhm-sidecar-parity-rehearsal.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sidecar_parity_rehearsal", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_disposable_rehearsal_projects_candidates_then_rolls_back(tmp_path: Path) -> None:
    result = _load_module().run_rehearsal(tmp_path)

    assert result["ok"] is True
    assert result["production_mutation"] is False
    assert result["staging_authorized"] is False
    assert result["parity_proven"] is False
    assert result["rollback_verified"] is True
    assert result["fixture_parity_proven"] is True
    assert result["graph"]["deterministic"] is True
    assert result["graph"]["edge_count"] == 1
    assert result["projected_candidate_counts"] == {"memory_links": 1, "memory_artifacts": 2}
    assert result["projected_graph_counts"] == {
        "task_graph_nodes": 2,
        "task_graph_edges": 1,
        "task_graph_snapshots": 1,
        "task_graph_current": 1,
    }
    assert result["post_rollback_counts"] == {
        "memory_links": 0,
        "memory_artifacts": 0,
        "task_graph_nodes": 0,
        "task_graph_edges": 0,
        "task_graph_snapshots": 0,
        "task_graph_current": 0,
    }
    assert result["blocked_sources"] == ["tasks.json"]


def test_builder_emits_explicit_dependency_edge_and_stable_material() -> None:
    module = _load_module()
    tasks = [
        {"id": "b", "task_id": "task-b", "title": "B", "status": "open", "dependencies": ["task-a"]},
        {"id": "a", "task_id": "task-a", "title": "A", "status": "open", "dependencies": []},
    ]

    graph = module.build_task_graph(tasks, project="blackholememory")

    assert len(graph["nodes"]) == 2
    assert len(graph["edges"]) == 1
    edge = graph["edges"][0]
    assert edge["relation"] == "depends_on"
    assert edge["source_node_key"].endswith(":task-b")
    assert edge["target_node_key"].endswith(":task-a")
    assert graph == module.build_task_graph(list(reversed(tasks)), project="blackholememory")


def test_builder_keeps_same_task_id_isolated_by_project() -> None:
    result = _load_module()._run_project_isolation_rehearsal()

    assert result["proven"] is True
    assert result["same_task_id"] is True
    assert result["node_keys_distinct"] is True
    assert result["snapshots_distinct"] is True
    assert result["graph_digests_distinct"] is True
    assert result["project_mismatch_rejected"] is True
    assert result["canonical_alias_rejected"] is True
    assert result["cross_project_dependency_rejected"] is True
    assert result["conflict_matrix_proven"] is True


@pytest.mark.parametrize(
    ("tasks", "message"),
    [
        (
            [
                {"id": "a", "task_id": "same", "title": "A", "status": "open"},
                {"id": "b", "task_id": "same", "title": "B", "status": "open"},
            ],
            "duplicate task_id",
        ),
        (
            [{"id": "a", "task_id": "a", "title": "A", "status": "open", "dependencies": ["missing"]}],
            "unknown dependency",
        ),
        (
            [
                {"id": "a", "task_id": "a", "title": "A", "status": "open"},
                {
                    "id": "b",
                    "task_id": "b",
                    "title": "B",
                    "status": "open",
                    "dependencies": [{"task_id": "a", "relation": "blocks"}],
                },
            ],
            "relation not allowed",
        ),
        (
            [{"id": "a", "task_id": "a", "title": "A", "status": "open", "dependencies": "a"}],
            "dependencies must be a list",
        ),
        (
            [
                {"id": "a", "task_id": "a", "title": "A", "status": "open"},
                {
                    "id": "b",
                    "task_id": "b",
                    "title": "B",
                    "status": "open",
                    "dependencies": ["a", "a"],
                },
            ],
            "duplicate dependency",
        ),
        (
            [
                {"id": "a", "task_id": "a", "project": "project-b", "title": "A", "status": "open"},
            ],
            "project mismatch",
        ),
        (
            [
                {
                    "id": "a",
                    "task_id": "a",
                    "project": "blackholememory",
                    "title": "A",
                    "status": "open",
                    "dependencies": [{"task_id": "a", "project": "project-b"}],
                },
            ],
            "dependency project mismatch",
        ),
    ],
)
def test_builder_rejects_conflicts_fail_closed(tasks: list[dict[str, object]], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _load_module().build_task_graph(tasks, project="blackholememory")
