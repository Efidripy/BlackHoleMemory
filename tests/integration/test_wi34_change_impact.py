from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app
from blackholememory.code_graph import build_code_graph
from blackholememory.convention_memory import build_convention_memory
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def test_change_impact_internal_api_is_read_only_and_hidden(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "module.py").write_text("def keep_value():\n    return 1\n", encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    database = runtime_dir / "live-memory" / "memories.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi34-api", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    graph = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    build_convention_memory(database, project="demo", root_id=state.root_id, graph_snapshot_id=graph["graph_snapshot_id"])
    monkeypatch.setattr(bhm_app.settings, "repo_root", root)
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_dir)

    route = next(item for item in bhm_app.app.routes if getattr(item, "path", "") == "/bhm/change-impact/preview")
    assert route.include_in_schema is False
    client = TestClient(bhm_app.app)
    response = client.post("/bhm/change-impact/preview", json={"project": "demo", "changed_paths": ["module.py"]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "bhm.change-impact.v1"
    assert payload["execution"]["writes_sqlite_state"] is False
    assert payload["execution"]["auto_apply"] is False
    assert payload["git_history"]["writes_worktree"] is False
