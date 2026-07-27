from __future__ import annotations

from blackholememory.bicep_module_resolution import build_bicep_module_resolution


def test_bicep_module_resolution_is_metadata_only_and_marks_unresolved() -> None:
    nodes = [
        {"node_id": "root", "node_kind": "file", "path": "main.bicep", "language": "bicep", "name": "main"},
        {"node_id": "decl", "node_kind": "module", "path": "main.bicep", "language": "bicep", "name": "network", "attributes": {"module_target": "./modules/network.bicep"}},
        {"node_id": "target", "node_kind": "file", "path": "modules/network.bicep", "language": "bicep", "name": "network"},
        {"node_id": "missing", "node_kind": "module", "path": "main.bicep", "language": "bicep", "name": "missing", "attributes": {"module_target": "./modules/missing.bicep"}},
    ]
    edges = [
        {"edge_kind": "imports", "source_node_id": "root", "target_node_id": "decl", "line": 2, "attributes": {"module_target": "./modules/network.bicep"}},
        {"edge_kind": "imports", "source_node_id": "root", "target_node_id": "missing", "line": 3, "attributes": {"module_target": "./modules/missing.bicep"}},
    ]
    result = build_bicep_module_resolution(nodes, edges, max_items=16)
    assert result["schema_version"] == "bhm.bicep-module-resolution.v1"
    assert result["resolved_count"] == 1
    assert result["unresolved_count"] == 1
    assert result["execution"]["proposal_only"] is True
    assert result["execution"]["compiler_or_lsp"] is False
    assert result["execution"]["raw_source_returned"] is False
    assert result["proposals"][0]["target_paths"] == ["modules/network.bicep"]
    assert all("content" not in item and "source" not in item for item in result["proposals"])


def test_bicep_module_resolution_is_deterministic_and_bounded() -> None:
    result = build_bicep_module_resolution([], [], max_items=9999)
    assert result["limits"]["max_items"] == 256
    assert result["digest"] == build_bicep_module_resolution([], [], max_items=256)["digest"]
