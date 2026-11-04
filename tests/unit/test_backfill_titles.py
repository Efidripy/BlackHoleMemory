from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "backfill_titles.py"
SPEC = importlib.util.spec_from_file_location("backfill_titles", SCRIPT)
backfill = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(backfill)


def make_db(path: Path, rows: list[tuple]) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE memories (
            memory_id TEXT PRIMARY KEY,
            project TEXT,
            memory_type TEXT,
            lifecycle TEXT,
            title TEXT,
            upsert_key TEXT,
            current_revision_id TEXT,
            created_at TEXT,
            metadata_json TEXT
        );
        CREATE TABLE memory_revisions (
            revision_id TEXT PRIMARY KEY,
            memory_id TEXT,
            content TEXT
        );
        """
    )
    for row in rows:
        connection.execute("INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row[:9])
        connection.execute("INSERT INTO memory_revisions VALUES (?, ?, ?)", (row[6], row[0], row[9]))
    connection.commit()
    connection.close()


def valid_row(memory_id: str, content: str | None = None, **overrides) -> tuple:
    content = (
        content
        if content is not None
        else "demo checkpoint:\ndone: Implemented login authentication middleware.\nnext: Deploy"
    )
    values = {
        "project": "demo",
        "memory_type": "checkpoint",
        "lifecycle": "active",
        "title": "demo checkpoint:",
        "upsert_key": memory_id,
        "revision_id": f"r-{memory_id}",
        "created_at": "2026-03-01T10:00:00Z",
        "metadata_json": json.dumps({"raw_title": "demo checkpoint:"}),
        "content": content,
    }
    values.update(overrides)
    return (
        memory_id,
        values["project"],
        values["memory_type"],
        values["lifecycle"],
        values["title"],
        values["upsert_key"],
        values["revision_id"],
        values["created_at"],
        values["metadata_json"],
        values["content"],
    )


def run_main(db: Path, out: Path, *extra: str) -> int:
    return backfill.main(["--db", str(db), "--out", str(out), *extra])


def test_derive_title_from_done(tmp_path: Path) -> None:
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    rows = [
        valid_row(
            "1",
            content="myproj checkpoint:\ndone: Fixed database connection pool timeout. Next was deploy.\nnext: deploy",
            project="myproj",
            created_at="2026-03-15T12:00:00Z",
        ),
        valid_row(
            "2",
            content="myproj checkpoint:\nDone:\n- Added user profile view\n- Updated nav bar\nnext: test",
            project="myproj",
            created_at="2026-04-20T08:00:00Z",
        ),
    ]
    make_db(db, rows)

    run_main(db, out)

    report = json.loads(out.read_text(encoding="utf-8"))
    assert len(report["items"]) == 2
    assert report["items"][0]["new_title"] == "2026-03-15 myproj: Fixed database connection pool timeout."
    assert report["items"][1]["new_title"] == "2026-04-20 myproj: Added user profile view"


def test_derive_title_from_body(tmp_path: Path) -> None:
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    rows = [
        valid_row(
            "1",
            content="myproj architecture:\nThe backend uses Kafka event streaming for asynchronous operations. Each topic has 3 partitions.",
            title="myproj architecture:",
            project="myproj",
            memory_type="architecture",
            created_at="2026-05-01T00:00:00Z",
        ),
        valid_row(
            "2",
            content="# demo workflow:\nConfigured automated deployment pipeline to staging cluster. Tests pass.",
            title="demo workflow:",
            project="demo",
            memory_type="workflow",
            created_at="2026-05-02T00:00:00Z",
        ),
        valid_row(
            "3",
            content="demo pattern:\nPattern: Factory method pattern is used to instantiate storage plugins dynamically.",
            title="demo pattern:",
            project="demo",
            memory_type="pattern",
            created_at="2026-05-03T00:00:00Z",
        ),
    ]
    make_db(db, rows)

    run_main(db, out)

    report = json.loads(out.read_text(encoding="utf-8"))
    assert len(report["items"]) == 3
    assert (
        report["items"][0]["new_title"]
        == "2026-05-01 myproj: The backend uses Kafka event streaming for asynchronous operations."
    )
    assert (
        report["items"][1]["new_title"]
        == "2026-05-02 demo: Configured automated deployment pipeline to staging cluster."
    )
    assert (
        report["items"][2]["new_title"]
        == "2026-05-03 demo: Factory method pattern is used to instantiate storage plugins dynamically."
    )


def test_skip_empty_or_meaningless(tmp_path: Path) -> None:
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    rows = [
        valid_row("1", content="demo checkpoint:\ndone:\nnext: deploy"),
        valid_row("2", content="demo checkpoint:\ncheckpoint:"),
        valid_row("3", content="demo checkpoint:\ndone: none"),
        valid_row("4", content="demo checkpoint:\ndone: n/a"),
        valid_row("5", content="demo checkpoint:\ndone: -"),
        valid_row("6", content="d checkpoint:\ndone: ok", project="d", created_at="2026-01-01"),
        valid_row("7", content=""),
    ]
    make_db(db, rows)

    run_main(db, out)

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["skipped"] == 7
    assert len(report["items"]) == 0


def test_idempotency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "memories.sqlite3"
    out1 = tmp_path / "report1.json"
    out2 = tmp_path / "report2.json"
    backup1 = tmp_path / "backup1.sqlite3"
    backup2 = tmp_path / "backup2.sqlite3"

    make_db(db, [valid_row("1")])
    monkeypatch.setenv("BHM_CALLER_TOKEN", "test-token")

    calls: list[dict] = []

    class Response:
        status_code = 200

    def mock_post(*args, **kwargs):
        calls.append(kwargs)
        # Simulate what BHM server does upon successful title update in SQLite
        conn = sqlite3.connect(db)
        conn.execute("UPDATE memories SET title = ? WHERE memory_id = ?", (kwargs["json"]["title"], kwargs["json"]["id"]))
        conn.commit()
        conn.close()
        return Response()

    monkeypatch.setattr(backfill.httpx, "post", mock_post)

    # First run: updates template title
    run_main(db, out1, "--apply", "--backup", str(backup1))
    report1 = json.loads(out1.read_text(encoding="utf-8"))
    assert len(report1["items"]) == 1
    assert len(calls) == 1
    new_title = report1["items"][0]["new_title"]
    assert not new_title.endswith(":")

    # Second run: already non-template title is skipped, changes nothing
    run_main(db, out2, "--apply", "--backup", str(backup2))
    report2 = json.loads(out2.read_text(encoding="utf-8"))
    assert len(report2["items"]) == 0
    assert len(calls) == 1  # No additional calls made


def test_raw_title_immutability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    backup = tmp_path / "backup.sqlite3"

    initial_metadata = json.dumps({"raw_title": "preserved_raw_title", "category": "infra"})
    make_db(db, [valid_row("1", metadata_json=initial_metadata)])
    monkeypatch.setenv("BHM_CALLER_TOKEN", "test-token")

    calls: list[dict] = []

    class Response:
        status_code = 200

    monkeypatch.setattr(backfill.httpx, "post", lambda *args, **kwargs: calls.append(kwargs) or Response())

    run_main(db, out, "--apply", "--backup", str(backup))

    assert len(calls) == 1
    payload = calls[0]["json"]
    # raw_title must never be in update payload
    assert "raw_title" not in payload
    assert payload["id"] == "1"
    assert payload["title"] == "2026-03-01 demo: Implemented login authentication middleware."

    # SQLite metadata_json must retain original raw_title
    conn = sqlite3.connect(db)
    meta = json.loads(conn.execute("SELECT metadata_json FROM memories WHERE memory_id = '1'").fetchone()[0])
    conn.close()
    assert meta["raw_title"] == "preserved_raw_title"


def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    make_db(db, [valid_row("1")])

    called = []
    monkeypatch.setattr(backfill.httpx, "post", lambda *args, **kwargs: called.append(args))

    run_main(db, out)

    assert called == []
    report = json.loads(out.read_text(encoding="utf-8"))
    assert len(report["items"]) == 1
    assert "status" not in report["items"][0]
    assert report["items"][0]["new_title"] == "2026-03-01 demo: Implemented login authentication middleware."


def test_update_only_targets_template_titles(tmp_path: Path) -> None:
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    rows = [
        valid_row("1", lifecycle="archived", title="demo checkpoint:"),
        valid_row("2", lifecycle="active", title="2026-03-01 demo: Already good title"),
        valid_row("3", lifecycle="active", title="Custom title without trailing colon"),
        valid_row("4", lifecycle="active", title="demo checkpoint:"),
        valid_row("5", lifecycle="active", title="demo hybrid session record:  "),
    ]
    make_db(db, rows)

    run_main(db, out)

    report = json.loads(out.read_text(encoding="utf-8"))
    assert [item["memory_id"] for item in report["items"]] == ["4", "5"]
    assert report["skipped_filtered"] == 3


def test_word_boundary_truncation(tmp_path: Path) -> None:
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    long_sentence = (
        "Investigated an extremely tricky race condition in background synchronization threads "
        "and applied fine-grained locking mechanisms to prevent database lockups under high concurrency."
    )
    make_db(db, [valid_row("1", content=f"demo checkpoint:\ndone: {long_sentence}\nnext: monitor")])

    run_main(db, out)

    report = json.loads(out.read_text(encoding="utf-8"))
    new_title = report["items"][0]["new_title"]
    assert len(new_title) <= 110
    assert not new_title.endswith(":")
    # Verify no broken word at boundary
    last_word = new_title.split()[-1]
    assert last_word in long_sentence


def test_project_filter(tmp_path: Path) -> None:
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    rows = [
        valid_row("1", project="alpha", title="alpha checkpoint:"),
        valid_row("2", project="beta", title="beta checkpoint:"),
    ]
    make_db(db, rows)

    run_main(db, out, "--project", "alpha")

    report = json.loads(out.read_text(encoding="utf-8"))
    assert [item["memory_id"] for item in report["items"]] == ["1"]
    assert report["skipped_filtered"] == 1


def test_apply_circuit_breaker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    backup = tmp_path / "backup.sqlite3"
    rows = [valid_row(str(i)) for i in range(1, 6)]
    make_db(db, rows)
    monkeypatch.setenv("BHM_CALLER_TOKEN", "test-token")

    calls: list[dict] = []

    class Response:
        status_code = 500

    monkeypatch.setattr(backfill.httpx, "post", lambda *args, **kwargs: calls.append(kwargs) or Response())

    run_main(db, out, "--apply", "--backup", str(backup))

    report = json.loads(out.read_text(encoding="utf-8"))
    assert len(calls) == 3
    statuses = [item["status"] for item in report["items"]]
    assert statuses == ["failed", "failed", "failed", "not_attempted", "not_attempted"]


def test_apply_requires_backup_and_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "memories.sqlite3"
    out = tmp_path / "report.json"
    make_db(db, [valid_row("1")])

    # Missing --backup
    with pytest.raises(SystemExit):
        run_main(db, out, "--apply")

    # Missing token
    monkeypatch.delenv("BHM_CALLER_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        run_main(db, out, "--apply", "--backup", str(tmp_path / "backup.sqlite3"))
