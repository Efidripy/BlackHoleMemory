from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blackholememory import projection_auto_requeue as module


class _HealthyQdrant:
    def get_collections(self):
        return []


class _UnhealthyQdrant:
    def get_collections(self):
        raise ConnectionError("qdrant unavailable")


def _database(tmp_path: Path, *errors: str) -> Path:
    database = tmp_path / "memories.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE memory_outbox (event_id TEXT PRIMARY KEY, status TEXT, attempts INTEGER, "
        "available_at TEXT, claimed_at TEXT, claim_token TEXT, last_error TEXT, updated_at TEXT)"
    )
    for index, error in enumerate(errors):
        connection.execute(
            "INSERT INTO memory_outbox VALUES (?, 'dead_letter', 5, '', NULL, NULL, ?, '')",
            (f"event-{index}", error),
        )
    connection.commit()
    connection.close()
    return database


def test_auto_requeue_whitelist_limit_health_and_backup(monkeypatch, tmp_path):
    database = _database(tmp_path, "qdrant unavailable", "binding_generation mismatch", "mystery")
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    controller = module.AutoRequeueController(database, runtime_dir=tmp_path, clock=lambda: now[0])
    seen = []

    def backup(source, target):
        seen.append(sqlite3.connect(source).execute("SELECT status FROM memory_outbox WHERE event_id='event-0'").fetchone()[0])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())

    monkeypatch.setattr(module, "_online_backup", backup)
    assert controller.run_pass(_HealthyQdrant())[0]["attempt"] == 1
    assert seen == ["dead_letter"]
    assert controller.run_pass(_HealthyQdrant()) == []

    now[0] += timedelta(minutes=5)
    connection = sqlite3.connect(database)
    connection.execute("UPDATE memory_outbox SET status='dead_letter' WHERE event_id='event-0'")
    connection.commit()
    connection.close()
    assert controller.run_pass(_HealthyQdrant())[0]["attempt"] == 2

    now[0] += timedelta(minutes=5)
    connection = sqlite3.connect(database)
    connection.execute("UPDATE memory_outbox SET status='dead_letter' WHERE event_id='event-0'")
    connection.commit()
    connection.close()
    assert controller.run_pass(_HealthyQdrant()) == []
    rows = sqlite3.connect(database).execute("SELECT event_id, status FROM memory_outbox ORDER BY event_id").fetchall()
    assert rows == [("event-0", "dead_letter"), ("event-1", "dead_letter"), ("event-2", "dead_letter")]


def test_auto_requeue_disabled_or_unhealthy_does_nothing(monkeypatch, tmp_path):
    database = _database(tmp_path, "qdrant timeout")
    monkeypatch.setenv("BHM_PROJECTION_AUTO_REQUEUE", "0")
    controller = module.AutoRequeueController(database, runtime_dir=tmp_path)
    assert controller.run_pass(_HealthyQdrant()) == []
    monkeypatch.delenv("BHM_PROJECTION_AUTO_REQUEUE")
    controller = module.AutoRequeueController(database, runtime_dir=tmp_path)
    assert controller.run_pass(_UnhealthyQdrant()) == []
    assert sqlite3.connect(database).execute("SELECT status FROM memory_outbox").fetchone() == ("dead_letter",)
    assert module.transient_reason("unknown error") is None


def test_transient_server_errors_require_http_context():
    for error in ("expected 768 got 512", "vector dimension 512", "collection has 5000 points"):
        assert module.transient_reason(error) is None
    for error in ("HTTP 503 Service Unavailable", "status_code=502", "Internal Server Error"):
        assert module.transient_reason(error) is not None
