from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


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
    written_manifest = json.loads((tmp_path / "backup" / "requeue-manifest.json").read_text(encoding="utf-8"))
    assert written_manifest["changed"] == 1
    status = sqlite3.connect(database).execute("SELECT status, attempts FROM memory_outbox").fetchone()
    assert status == ("pending", 0)


def test_requeue_rejects_hardlinked_manifest_before_live_mutation(tmp_path):
    module = _module()
    database = tmp_path / "memories.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE memory_outbox (event_id TEXT, status TEXT, attempts INTEGER, available_at TEXT, claimed_at TEXT, claim_token TEXT, last_error TEXT, updated_at TEXT)"
    )
    connection.execute("INSERT INTO memory_outbox VALUES ('evt', 'dead_letter', 1, '', NULL, NULL, '', '')")
    connection.commit()
    connection.close()

    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text("sentinel", encoding="utf-8")
    manifest_path = backup_root / "requeue-manifest.json"
    try:
        manifest_path.hardlink_to(sentinel)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        module._apply(database, backup_root, module._event_digest(["evt"]))

    assert sqlite3.connect(database).execute("SELECT status FROM memory_outbox").fetchone() == ("dead_letter",)
    assert sentinel.read_text(encoding="utf-8") == "sentinel"
    assert not (backup_root / "memories.sqlite3").exists()


def test_requeue_rejects_hardlinked_backup_target_before_live_mutation(tmp_path):
    module = _module()
    database = tmp_path / "memories.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE memory_outbox (event_id TEXT, status TEXT, attempts INTEGER, available_at TEXT, claimed_at TEXT, claim_token TEXT, last_error TEXT, updated_at TEXT)"
    )
    connection.execute("INSERT INTO memory_outbox VALUES ('evt', 'dead_letter', 1, '', NULL, NULL, '', '')")
    connection.commit()
    connection.close()

    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    sentinel = tmp_path / "sentinel.sqlite3"
    sentinel.write_bytes(b"sentinel")
    try:
        (backup_root / database.name).hardlink_to(sentinel)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        module._apply(database, backup_root, module._event_digest(["evt"]))

    assert sqlite3.connect(database).execute("SELECT status FROM memory_outbox").fetchone() == ("dead_letter",)
    assert sentinel.read_bytes() == b"sentinel"


def test_requeue_rejects_symlinked_backup_root_before_live_mutation(tmp_path):
    module = _module()
    database = tmp_path / "memories.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE memory_outbox (event_id TEXT, status TEXT, attempts INTEGER, available_at TEXT, claimed_at TEXT, claim_token TEXT, last_error TEXT, updated_at TEXT)"
    )
    connection.execute("INSERT INTO memory_outbox VALUES ('evt', 'dead_letter', 1, '', NULL, NULL, '', '')")
    connection.commit()
    connection.close()

    outside = tmp_path / "outside"
    outside.mkdir()
    backup_root = tmp_path / "backup-link"
    try:
        backup_root.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(OSError, match="symlink|reparse"):
        module._apply(database, backup_root, module._event_digest(["evt"]))

    assert sqlite3.connect(database).execute("SELECT status FROM memory_outbox").fetchone() == ("dead_letter",)
    assert not list(outside.iterdir())
