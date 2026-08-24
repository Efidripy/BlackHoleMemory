from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bhm-create-external-live-backup.py"
SPEC = importlib.util.spec_from_file_location("external_live_backup", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO records(value) VALUES ('ok')")


def test_create_backup_uses_verified_sqlite_snapshots_and_copies_json(tmp_path: Path) -> None:
    source = tmp_path / "live-memory"
    source.mkdir()
    for name in MODULE.SQLITE_NAMES:
        _database(source / name)
    (source / "tasks.json").write_text('{"task":"ready"}\n', encoding="utf-8")

    destination = tmp_path / "external" / "backup-1"
    destination.parent.mkdir()
    report = MODULE.create_backup(source, destination)

    assert len(report["sqlite_online_backups"]) == 3
    assert {row["quick_check"] for row in report["sqlite_online_backups"]} == {"ok"}
    assert (destination / "tasks.json").read_text(encoding="utf-8") == '{"task":"ready"}\n'
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_digest"] == report["manifest_digest"]


def test_create_backup_rejects_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "live-memory"
    source.mkdir()
    destination = tmp_path / "backup"
    destination.mkdir()

    try:
        MODULE.create_backup(source, destination)
    except MODULE.ExternalBackupError as exc:
        assert "destination must not exist" in str(exc)
    else:
        raise AssertionError("existing destination was accepted")
