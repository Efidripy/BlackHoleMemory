from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from blackholememory.memory_repository import SQLiteMemoryRepository

import bhm_launcher
import bhm_launcher_dashboard
import bhm_launcher_operator


ACTION_KEYS = [item.key for item in bhm_launcher.OPERATOR_ACTIONS]


class _PreviewHarness:
    def __init__(self, project: str, path: str) -> None:
        self._project = project
        self._path = path

    def operator_project(self) -> str:
        return self._project

    def operator_path(self) -> str:
        return self._path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def operator_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, Path]:
    root = tmp_path / "project"
    root.mkdir()
    database = root / "memories.sqlite3"
    SQLiteMemoryRepository(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO memory_outbox(
                event_id, aggregate_type, aggregate_id, event_type, event_version,
                payload_json, status, attempts, available_at, claimed_at,
                claim_token, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt-dead-letter",
                "memory",
                "memory-1",
                "memory.created",
                1,
                "{}",
                "dead_letter",
                2,
                "",
                None,
                None,
                "qdrant offline",
                "",
                "",
            ),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    runtime = root / ".runtime"
    backups = runtime / "backups" / "fixture"
    backups.mkdir(parents=True)
    restore_source = backups / "memories.sqlite3"
    shutil.copy2(database, restore_source)
    export_root = runtime / "admin-exports"
    export_root.mkdir(parents=True)
    export_path = export_root / "fixture.json"
    export_path.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(bhm_launcher, "find_project_root", lambda: root)
    monkeypatch.setattr(bhm_launcher, "_operator_database_path", lambda: database)
    monkeypatch.setattr(bhm_launcher, "_operator_runtime_root", lambda name: runtime / name)
    monkeypatch.setattr(bhm_launcher, "_assert_owned_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bhm_launcher, "_assert_launchable_source", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bhm_launcher, "has_virtualenv", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(bhm_launcher, "host_python_executable", lambda: "python")

    def load_requeue_module():
        script = SCRIPTS_ROOT / "requeue-bhm-dead-letters.py"
        spec = importlib.util.spec_from_file_location("smoke_requeue_bhm_dead_letters", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    monkeypatch.setattr(bhm_launcher_operator, "_requeue_script_module", load_requeue_module)

    def fake_json_request(url: str, **_kwargs):
        if "governed-consolidation/proposals" in url:
            data = {"proposals": []}
        elif "admin/import-preview" in url:
            data = {"phase": "preview", "valid": True}
        elif "integrity-audit" in url:
            data = {"healthy": True}
        elif "integrity/repair-strict" in url:
            data = {"repaired": 0}
        elif "qdrant-catalog" in url:
            data = {"collections": []}
        else:
            data = {}
        return SimpleNamespace(ok=True, status_code=200, error="", data=data)

    monkeypatch.setattr(bhm_launcher, "safe_json_request", fake_json_request)

    def fake_operator_json(args, **_kwargs):
        joined = " ".join(str(item) for item in args)
        if "bhm-sqlite-retention.py" in joined:
            assert str(database) in [str(item) for item in args]
            return {
                "ok": True,
                "retention": {"plan_digest": "digest", "as_of": "2026-01-01T00:00:00Z"},
            }
        if "bhm_reconcile_projection.py" in joined:
            return {
                "ok": True,
                "controlPlane": {"rebuildEligibility": "eligible"},
                "planDigest": "digest",
                "asOf": "2026-01-01T00:00:00Z",
            }
        if "recover-qdrant.ps1" in joined:
            return {"ok": True, "recovered": False}
        if "bhm-projection-operator.ps1" in joined:
            return {"ok": True, "action": "dry-run"}
        return {"ok": True}

    monkeypatch.setattr(bhm_launcher, "_run_operator_json", fake_operator_json)
    monkeypatch.setattr(
        bhm_launcher,
        "_run_operator_report",
        lambda *_args, **_kwargs: {"ok": True, "items": [], "plan": {}},
    )
    return database, str(export_path), restore_source


def test_all_operator_previews_are_read_only_and_shape_stable(
    operator_fixture: tuple[Path, str, Path],
) -> None:
    database, export_path, restore_source = operator_fixture

    for key in ACTION_KEYS:
        before = _sha256(database)
        path = str(restore_source) if key == "restore" else export_path
        harness = _PreviewHarness("demo", path)
        operation = bhm_launcher_dashboard.DashboardScreen._operator_preview_operation(harness, key)
        payload = operation()
        assert isinstance(payload, dict)
        assert payload["action"] == key
        json.dumps(payload)
        assert _sha256(database) == before, key


def test_backup_and_requeue_apply_on_fixture_database(
    operator_fixture: tuple[Path, str, Path],
) -> None:
    database, _export_path, _restore_source = operator_fixture

    backup = bhm_launcher_operator.operator_sqlite_backup("demo")
    assert backup["action"] == "backup"
    assert Path(backup["path"]).is_file()

    preview = bhm_launcher_operator.operator_requeue_preview("demo")
    result = bhm_launcher_operator.operator_requeue_apply(preview, "demo")
    assert result["action"] == "requeue"
    assert result["phase"] == "apply"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_outbox WHERE status = 'dead_letter'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_outbox WHERE status = 'pending'"
        ).fetchone()[0] == 1
