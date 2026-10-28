from pathlib import Path

from blackholememory.code_graph import build_code_graph
from blackholememory.code_graph_query import query_code_graph
from blackholememory.repository_index import RepositorySourceProvenance, index_repository, probe_repository_state


def test_callers_returns_edges_within_time_budget(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "module.py").write_text("def target():\n    pass\n\ndef caller():\n    target()\n", encoding="utf-8")
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://budget", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    root_id = probe_repository_state(root, project="demo").root_id
    build_code_graph(database, project="demo", root_id=root_id, repository_snapshot_id=indexed["snapshot_id"])

    result = query_code_graph(database, project="demo", root_id=root_id, operation="callers", query="target", depth=1, time_budget_ms=250)
    nodes = {node["node_id"]: node["name"] for node in result["nodes"]}
    assert any(nodes[edge["source_node_id"]] == "caller" and nodes[edge["target_node_id"]] == "target" for edge in result["edges"])
    assert not result["bounds"]["budget_exceeded"]
    assert result["bounds"]["elapsed_ms"] < 250
