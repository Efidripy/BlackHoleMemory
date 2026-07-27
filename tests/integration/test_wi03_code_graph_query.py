from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app
from blackholememory.code_graph import build_code_graph
from blackholememory.code_graph_query import query_code_graph
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def test_query_marks_explicit_old_graph_snapshot_stale(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "module.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi03", license="MIT", evidence_class="E0")
    first_index = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    first_graph = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=first_index["snapshot_id"])
    (root / "module.py").write_text("def keep():\n    return 2\n\ndef added():\n    return 3\n", encoding="utf-8")
    second_index = index_repository(root, database, project="demo", source=source)
    build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=second_index["snapshot_id"])
    response = query_code_graph(database, project="demo", root_id=state.root_id, operation="symbol", query="keep", snapshot_id=first_graph["graph_snapshot_id"])
    assert response["stale"] is True
    assert response["snapshot_id"] == first_graph["graph_snapshot_id"]


def test_internal_fastapi_query_and_explain_are_read_only(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "module.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    database = runtime_dir / "live-memory" / "memories.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi03-api", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)
    client = TestClient(bhm_app.app)

    query = client.post("/bhm/code-graph/query", json={"project": "demo", "operation": "symbol", "query": "keep"})
    explain = client.post("/bhm/code-graph/explain", json={"project": "demo", "operation": "symbol", "query": "keep"})

    assert query.status_code == 200
    assert query.json()["schema_version"] == "bhm.code-graph.query.v1"
    assert query.json()["execution"]["writes_sqlite_state"] is False
    assert explain.status_code == 200
    assert explain.json()["schema_version"] == "bhm.code-graph.explain.v1"
    assert explain.json()["explanations"]
