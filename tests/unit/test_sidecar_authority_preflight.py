from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-bhm-sidecar-authority.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sidecar_authority_preflight", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sidecar_preflight_is_read_only_and_reports_split_brain(tmp_path: Path) -> None:
    module = _load_module()
    live = tmp_path / ".runtime" / "live-memory"
    live.mkdir(parents=True)
    (live / "slots.json").write_text(json.dumps([{"label": "x"}, {"label": "x"}]), encoding="utf-8")
    database = live / "memories.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        "CREATE TABLE memories (id TEXT); CREATE TABLE memory_outbox (status TEXT);"
        "INSERT INTO memories VALUES ('m1'); INSERT INTO memory_outbox VALUES ('completed');"
    )
    connection.commit()
    connection.close()

    before = database.read_bytes()
    report = module.build_report(tmp_path)
    assert report["ok"] is True
    assert report["read_only"] is True
    assert report["authority_state"] == "sqlite_authoritative_with_sidecar_residual"
    assert report["reconciliation_ready"] is False
    assert report["migration_required"] is True
    assert report["split_brain_risk"] is True
    assert report["sidecar_count"] == 1
    assert report["duplicate_key_files"] == ["slots.json"]
    assert database.read_bytes() == before


def test_sidecar_preflight_fails_closed_on_invalid_json(tmp_path: Path) -> None:
    module = _load_module()
    live = tmp_path / ".runtime" / "live-memory"
    live.mkdir(parents=True)
    (live / "slots.json").write_text("{not-json", encoding="utf-8")
    assert module.build_report(tmp_path)["ok"] is False


def test_sidecar_preflight_marks_clean_sqlite_as_reconciliation_ready(tmp_path: Path) -> None:
    module = _load_module()
    live = tmp_path / ".runtime" / "live-memory"
    live.mkdir(parents=True)
    database = live / "memories.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript("CREATE TABLE memories (id TEXT); CREATE TABLE memory_outbox (status TEXT);")
    connection.commit()
    connection.close()

    report = module.build_report(tmp_path)
    assert report["ok"] is True
    assert report["authority_state"] == "sqlite_authoritative"
    assert report["reconciliation_ready"] is True
    assert report["migration_required"] is False
