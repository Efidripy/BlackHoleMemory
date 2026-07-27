from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from blackholememory.code_graph import SQLiteCodeGraphStore
from blackholememory.parser_activation import PARSER_REGISTRY_DIGEST
from blackholememory.parser_activation import ParserActivationError
from blackholememory.parser_activation import activate_parser_v2
from blackholememory.parser_activation import online_backup
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def _fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (root / "web.ts").write_text("export async function load() { return 1; }\n", encoding="utf-8")
    database = tmp_path / "memories.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://fixture", license="MIT", evidence_class="E0")
    index_repository(root, database, project="demo", source=source)
    return root, database, str(probe_repository_state(root, project="demo").root_id)


def test_parser_v2_activation_is_backed_up_and_published(tmp_path: Path) -> None:
    root, database, _root_id = _fixture(tmp_path)
    backup = tmp_path / "rollback" / "memories-before.sqlite3"
    report = activate_parser_v2(database, root=root, project="demo", backup=backup)

    assert report["ok"] is True
    assert report["parser_registry_digest"] == PARSER_REGISTRY_DIGEST
    assert report["after"]["parser_registry_digest"] == PARSER_REGISTRY_DIGEST
    assert report["after"]["previous_graph_snapshot_id"] is None
    assert report["summary"]["parser_error_count"] == 0
    assert backup.exists()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    current = SQLiteCodeGraphStore(database).current_snapshot("demo", report["after"]["root_id"])
    assert current is not None
    assert current["parser_registry_digest"] == PARSER_REGISTRY_DIGEST


def test_online_backup_refuses_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('ok')")
    target = tmp_path / "backup.sqlite3"
    online_backup(source, target)
    with pytest.raises(ParserActivationError, match="backup already exists"):
        online_backup(source, target)
