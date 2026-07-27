from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app
from blackholememory.code_graph import build_code_graph
from blackholememory.convention_memory import build_convention_memory
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def test_convention_memory_internal_preview_api_is_hidden_and_read_only(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "module.py").write_text("def keep_value():\n    return 1\n", encoding="utf-8")
    database = tmp_path / "runtime" / "live-memory" / "memories.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi04-api", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    graph = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    build_convention_memory(database, project="demo", root_id=state.root_id, graph_snapshot_id=graph["graph_snapshot_id"])
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", tmp_path / "runtime")
    client = TestClient(bhm_app.app)
    response = client.post("/bhm/conventions/preview", json={"project": "demo"})
    assert response.status_code == 200
    assert response.json()["schema_version"] == "bhm.repository-conventions.v1"
    assert response.json()["execution"]["writes_sqlite_state"] is False
