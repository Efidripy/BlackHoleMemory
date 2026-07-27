from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "requeue-bhm-dead-letters.py"


def _module():
    spec = importlib.util.spec_from_file_location("requeue_bhm_dead_letters", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dead_letter_requeue_is_backup_first_and_bounded(tmp_path):
    module = _module()
    database = tmp_path / "memories.sqlite3"
    connection = sqlite3.connect(database)
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
    connection.execute(
        "INSERT INTO memory_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("evt_dead", "dead_letter", 5, "2026-01-01T00:00:00Z", None, None, "Connection error.", "2026-01-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    ids, error = module._read_dead_letters(database)
    assert error is None
    assert ids == ["evt_dead"]

    manifest = module._apply(database, tmp_path / "backup", module._event_digest(ids))

    assert manifest["changed"] == 1
    assert (tmp_path / "backup" / "memories.sqlite3").exists()
    status = sqlite3.connect(database).execute("SELECT status, attempts FROM memory_outbox").fetchone()
    assert status == ("pending", 0)
