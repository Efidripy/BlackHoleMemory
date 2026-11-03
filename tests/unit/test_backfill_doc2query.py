from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "backfill_doc2query.py"
SPEC = importlib.util.spec_from_file_location("backfill_doc2query", SCRIPT)
backfill = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(backfill)


def make_db(path: Path, rows: list[tuple]) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE memories (
            memory_id TEXT PRIMARY KEY, project TEXT, memory_type TEXT,
            lifecycle TEXT, title TEXT, upsert_key TEXT,
            current_revision_id TEXT
        );
        CREATE TABLE memory_revisions (
            revision_id TEXT PRIMARY KEY, memory_id TEXT, content TEXT
        );
        """
    )
    for row in rows:
        connection.execute("INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?)", row[:7])
        connection.execute("INSERT INTO memory_revisions VALUES (?, ?, ?)", (row[6], row[0], row[7]))
    connection.commit()
    connection.close()


def valid_row(memory_id: str, content: str | None = None, **overrides):
    content = content or ("alpha title\n" + "knowledge " * 40)
    values = {
        "project": "demo", "memory_type": "fact", "lifecycle": "active",
        "title": "alpha title", "upsert_key": memory_id, "revision_id": f"r-{memory_id}",
        "content": content,
    }
    values.update(overrides)
    return (memory_id, values["project"], values["memory_type"], values["lifecycle"],
            values["title"], values["upsert_key"], values["revision_id"], values["content"])


def run_main(db: Path, out: Path, *extra: str):
    return backfill.main(["--db", str(db), "--out", str(out), *extra])


def test_candidate_filters_and_title_mismatch(tmp_path):
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    rows = [
        valid_row("1"),
        valid_row("2", memory_type="workflow"),
        valid_row("3", content="short"),
        valid_row("4", content="alpha title\n" + "Вопросы: already\n" + "x" * 210),
        valid_row("5", title="wrong title"),
    ]
    make_db(db, rows)
    backfill.enrich_content = lambda title, content: content + "\n\nВопросы: Что такое alpha title?"

    run_main(db, out)

    report = json.loads(out.read_text(encoding="utf-8"))
    assert [item["memory_id"] for item in report["items"]] == ["1"]
    assert report["skipped_filtered"] == 3
    assert report["skipped_title_mismatch"] == 1
    assert report["items"][0]["questions"] == "Вопросы: Что такое alpha title?"


def test_dry_run_does_not_call_api(tmp_path, monkeypatch):
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    make_db(db, [valid_row("1")])
    monkeypatch.setattr(backfill, "enrich_content", lambda title, content: content + "\n\nВопросы: Q?")
    called = []
    monkeypatch.setattr(backfill.httpx, "post", lambda *args, **kwargs: called.append(args))

    run_main(db, out)

    assert called == []


def test_apply_requires_backup(tmp_path):
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    make_db(db, [valid_row("1")])
    with pytest.raises(SystemExit):
        run_main(db, out, "--apply")


def test_apply_creates_backup_and_updates(tmp_path, monkeypatch):
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    backup = tmp_path / "backup.sqlite3"
    make_db(db, [valid_row("1")])
    monkeypatch.setenv("BHM_CALLER_TOKEN", "test-token")
    monkeypatch.setattr(backfill, "enrich_content", lambda title, content: content + "\n\nВопросы: Q?")
    calls = []

    class Response:
        status_code = 200

    monkeypatch.setattr(backfill.httpx, "post", lambda *args, **kwargs: calls.append(kwargs) or Response())

    run_main(db, out, "--apply", "--backup", str(backup))

    assert backup.exists()
    assert sqlite3.connect(backup).execute("SELECT count(*) FROM memories").fetchone() == (1,)
    assert len(calls) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["items"][0]["status"] == "ok"


def test_apply_stops_after_three_errors(tmp_path, monkeypatch):
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    backup = tmp_path / "backup.sqlite3"
    make_db(db, [valid_row(str(index)) for index in range(1, 6)])
    monkeypatch.setenv("BHM_CALLER_TOKEN", "test-token")
    monkeypatch.setattr(backfill, "enrich_content", lambda title, content: content + "\n\nВопросы: Q?")
    calls = []

    class Response:
        status_code = 500

    monkeypatch.setattr(backfill.httpx, "post", lambda *args, **kwargs: calls.append(args) or Response())

    run_main(db, out, "--apply", "--backup", str(backup))

    report = json.loads(out.read_text(encoding="utf-8"))
    assert len(calls) == 3
    assert [item["status"] for item in report["items"]] == ["failed"] * 3 + ["not_attempted"] * 2
