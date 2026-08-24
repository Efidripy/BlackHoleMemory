from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from blackholememory.code_graph import CodeGraphInjectedFailure
from blackholememory.code_graph import SQLiteCodeGraphStore
from blackholememory.code_graph import build_code_graph
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def _repo(tmp_path: Path) -> tuple[Path, Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "module.py").write_text("def keep():\n    return 1\n\ndef remove_me():\n    return 2\n", encoding="utf-8")
    database = tmp_path / "canonical.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi02", license="MIT", evidence_class="E0")
    first = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    return root, database, str(state.root_id), str(first["snapshot_id"])


def test_graph_uses_same_sqlite_authority_and_lkg_on_publish_failure(tmp_path: Path) -> None:
    root, database, root_id, snapshot_id = _repo(tmp_path)
    with pytest.raises(CodeGraphInjectedFailure):
        build_code_graph(database, project="demo", root_id=root_id, repository_snapshot_id=snapshot_id, fail_before_publish=True)
    assert SQLiteCodeGraphStore(database).current_snapshot("demo", root_id) is None

    first = build_code_graph(database, project="demo", root_id=root_id, repository_snapshot_id=snapshot_id)
    (root / "module.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
    second_index = index_repository(root, database, project="demo", source=RepositorySourceProvenance(owner="fixture", source_url="local://wi02", license="MIT", evidence_class="E0"))
    second = build_code_graph(database, project="demo", root_id=root_id, repository_snapshot_id=second_index["snapshot_id"])

    assert second["graph_snapshot_id"] != first["graph_snapshot_id"]
    old = SQLiteCodeGraphStore(database).snapshot(first["graph_snapshot_id"], include_material=True)
    current = SQLiteCodeGraphStore(database).current_snapshot("demo", root_id, include_material=True)
    assert current is not None
    old_names = {node["name"] for node in old["nodes"]}
    current_names = {node["name"] for node in current["nodes"]}
    assert "remove_me" in old_names
    assert "remove_me" not in current_names
    with sqlite3.connect(database) as connection:
        memory_like = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "repository_code_graph_current" in memory_like
