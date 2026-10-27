from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import bhm_launcher
import bhm_launcher_operator


def _database(path):
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE memory_outbox (
            event_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            available_at TEXT NOT NULL,
            claimed_at TEXT,
            claim_token TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.executemany(
        "INSERT INTO memory_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("evt-a", "dead_letter", 3, "", None, None, "qdrant offline", ""),
            ("evt-b", "dead_letter", 2, "", None, None, "qdrant offline", ""),
        ],
    )
    connection.commit()
    connection.close()
    return path


def _patch_operator_paths(monkeypatch, database, tmp_path):
    backup_root = tmp_path / "runtime" / "backups"
    monkeypatch.setattr(bhm_launcher, "_operator_database_path", lambda: database)
    monkeypatch.setattr(bhm_launcher, "_operator_runtime_root", lambda _name: backup_root)
    monkeypatch.setattr(bhm_launcher, "_assert_owned_path", lambda *_args, **_kwargs: None)


def test_requeue_preview_is_read_only_and_reports_error_counts(tmp_path, monkeypatch):
    database = _database(tmp_path / "memories.sqlite3")
    _patch_operator_paths(monkeypatch, database, tmp_path)

    before = sqlite3.connect(database).execute("SELECT status, attempts FROM memory_outbox ORDER BY event_id").fetchall()
    preview = bhm_launcher_operator.operator_requeue_preview("demo")
    after = sqlite3.connect(database).execute("SELECT status, attempts FROM memory_outbox ORDER BY event_id").fetchall()

    assert preview["mode"] == "ro"
    assert preview["dead_letter_count"] == 2
    assert preview["last_errors"] == [{"last_error": "qdrant offline", "count": 2}]
    assert before == after


def test_requeue_apply_requeues_after_backup(tmp_path, monkeypatch):
    database = _database(tmp_path / "memories.sqlite3")
    _patch_operator_paths(monkeypatch, database, tmp_path)

    preview = bhm_launcher_operator.operator_requeue_preview("demo")
    result = bhm_launcher_operator.operator_requeue_apply(preview, "demo")

    assert result["phase"] == "apply"
    assert result["backup"]
    assert result["manifest"]["changed"] == 2
    assert sqlite3.connect(database).execute("SELECT COUNT(*) FROM memory_outbox WHERE status = 'dead_letter'").fetchone()[0] == 0
    assert sqlite3.connect(database).execute("SELECT COUNT(*) FROM memory_outbox WHERE status = 'pending'").fetchone()[0] == 2


def test_requeue_apply_rejects_stale_digest_without_changes(tmp_path, monkeypatch):
    database = _database(tmp_path / "memories.sqlite3")
    _patch_operator_paths(monkeypatch, database, tmp_path)

    preview = bhm_launcher_operator.operator_requeue_preview("demo")
    connection = sqlite3.connect(database)
    connection.execute("UPDATE memory_outbox SET event_id = 'evt-z' WHERE event_id = 'evt-a'")
    connection.commit()
    connection.close()

    try:
        bhm_launcher_operator.operator_requeue_apply(preview, "demo")
    except RuntimeError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("stale digest must be rejected")
    assert sqlite3.connect(database).execute("SELECT COUNT(*) FROM memory_outbox WHERE status = 'dead_letter'").fetchone()[0] == 2
    assert not (tmp_path / "runtime" / "backups").exists()
