"""Unit tests for scripts/bhm_memory_inventory.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest

# Ensure scripts directory is in path for direct import
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bhm_memory_inventory as inv  # noqa: E402


def _create_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE memories (
                memory_id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                lifecycle TEXT NOT NULL DEFAULT 'active',
                title TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE memory_revisions (
                revision_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                content TEXT,
                content_sha256 TEXT,
                created_at TEXT NOT NULL
            )
        """)


def _insert_memory(
    path: Path,
    *,
    memory_id: str,
    project: str,
    memory_type: str,
    lifecycle: str = "active",
    title: str = "Test Title",
    created_at: str = "2026-10-01T10:00:00Z",
    updated_at: str = "2026-10-01T10:00:00Z",
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO memories (memory_id, project, memory_type, lifecycle, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (memory_id, project, memory_type, lifecycle, title, created_at, updated_at),
        )
        conn.execute(
            "INSERT INTO memory_revisions (revision_id, memory_id, content, content_sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"rev_{memory_id}", memory_id, "test content", "sha256_mock", created_at),
        )


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_inventory_counts_breakdown_and_shares(tmp_path: Path) -> None:
    db_path = tmp_path / "test_inventory.sqlite3"
    _create_database(db_path)

    # 6 knowledge records
    for i, mtype in enumerate(["decision", "pattern", "architecture", "rule", "playbook", "workflow"]):
        _insert_memory(db_path, memory_id=f"k_{i}", project="alpha", memory_type=mtype, created_at=f"2026-10-0{i+1}T10:00:00Z")

    # 2 noise records
    _insert_memory(db_path, memory_id="n_1", project="alpha", memory_type="log")
    _insert_memory(db_path, memory_id="n_2", project="alpha", memory_type="receipt")

    # 2 other records
    _insert_memory(db_path, memory_id="o_1", project="alpha", memory_type="custom_a")
    _insert_memory(db_path, memory_id="o_2", project="alpha", memory_type="custom_b")

    # Inactive records (archived, tombstoned, deleted) - must NOT be counted in active records
    _insert_memory(db_path, memory_id="arc_1", project="alpha", memory_type="decision", lifecycle="archived")
    _insert_memory(db_path, memory_id="tomb_1", project="alpha", memory_type="log", lifecycle="tombstoned")
    _insert_memory(db_path, memory_id="del_1", project="alpha", memory_type="pattern", lifecycle="deleted")

    report = inv.build_inventory(db_path, project="alpha")

    assert report["schema"] == inv.SCHEMA_VERSION
    assert report["database"] == "test_inventory.sqlite3"
    assert len(report["projects"]) == 1

    proj = report["projects"][0]
    assert proj["project"] == "alpha"
    assert proj["active_count"] == 10  # 6 knowledge + 2 noise + 2 other (archived excluded)

    # Shares
    sh = proj["shares"]
    assert sh["knowledge_count"] == 6
    assert sh["knowledge_ratio"] == 0.6
    assert sh["noise_count"] == 2
    assert sh["noise_ratio"] == 0.2
    assert sh["other_count"] == 2
    assert sh["other_ratio"] == 0.2

    # Type breakdown: ordered by count descending
    assert proj["type_counts"]["decision"] == 1
    assert len(proj["types"]) == 10

    # Oldest and newest dates
    assert proj["oldest_record_at"] == "2026-10-01T10:00:00Z"

    # Totals
    assert report["totals"]["projects_count"] == 1
    assert report["totals"]["active_count"] == 10
    assert report["totals"]["knowledge_count"] == 6
    assert report["totals"]["noise_count"] == 2
    assert report["totals"]["other_count"] == 2


def test_warnings_trigger_and_suppress(tmp_path: Path) -> None:
    db_path = tmp_path / "test_warnings.sqlite3"
    _create_database(db_path)

    now = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
    old_date = (now - timedelta(days=20)).isoformat()
    fresh_date = (now - timedelta(days=2)).isoformat()

    # Project "sparse":
    # - knowledge < 5 (only 2: workflow, decision) -> triggers low_knowledge
    # - 0 playbook/rule -> triggers no_playbook_rule
    # - stale state (> 14 days) -> triggers stale_state
    # - 7 noise records out of 10 total active (70% noise is not > 70%, add 1 more noise to make 8/11 = 72.7%) -> triggers high_noise
    _insert_memory(db_path, memory_id="sp_k1", project="sparse", memory_type="workflow", updated_at=fresh_date)
    _insert_memory(db_path, memory_id="sp_k2", project="sparse", memory_type="decision", updated_at=fresh_date)
    _insert_memory(db_path, memory_id="sp_stale", project="sparse", memory_type="state", updated_at=old_date)
    for i in range(8):
        _insert_memory(db_path, memory_id=f"sp_n{i}", project="sparse", memory_type="log", updated_at=fresh_date)

    report_sparse = inv.build_inventory(db_path, project="sparse", as_of=now, stale_days=14)
    sparse_proj = report_sparse["projects"][0]
    sparse_codes = {w["code"] for w in sparse_proj["warnings"]}
    assert "low_knowledge" in sparse_codes
    assert "no_playbook_rule" in sparse_codes
    assert "stale_state" in sparse_codes
    assert "high_noise" in sparse_codes

    # Project "healthy":
    # - 5 knowledge records (decision, rule, playbook, architecture, pattern) -> suppresses low_knowledge (5 >= 5)
    # - has rule and playbook -> suppresses no_playbook_rule
    # - state is fresh (2 days old) -> suppresses stale_state
    # - noise is 1 out of 6 (16.7% <= 70%) -> suppresses high_noise
    _insert_memory(db_path, memory_id="h_1", project="healthy", memory_type="decision", updated_at=fresh_date)
    _insert_memory(db_path, memory_id="h_2", project="healthy", memory_type="rule", updated_at=fresh_date)
    _insert_memory(db_path, memory_id="h_3", project="healthy", memory_type="playbook", updated_at=fresh_date)
    _insert_memory(db_path, memory_id="h_4", project="healthy", memory_type="architecture", updated_at=fresh_date)
    _insert_memory(db_path, memory_id="h_5", project="healthy", memory_type="state", updated_at=fresh_date)
    _insert_memory(db_path, memory_id="h_6", project="healthy", memory_type="log", updated_at=fresh_date)

    report_healthy = inv.build_inventory(db_path, project="healthy", as_of=now, stale_days=14)
    healthy_proj = report_healthy["projects"][0]
    healthy_codes = {w["code"] for w in healthy_proj["warnings"]}
    assert "low_knowledge" not in healthy_codes
    assert "no_playbook_rule" not in healthy_codes
    assert "stale_state" not in healthy_codes
    assert "high_noise" not in healthy_codes


def test_project_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "test_filter.sqlite3"
    _create_database(db_path)

    _insert_memory(db_path, memory_id="p1_1", project="proj_one", memory_type="fact")
    _insert_memory(db_path, memory_id="p2_1", project="proj_two", memory_type="fact")

    # All projects
    report_all = inv.build_inventory(db_path)
    assert len(report_all["projects"]) == 2
    assert {p["project"] for p in report_all["projects"]} == {"proj_one", "proj_two"}

    # Filtered project
    report_filtered = inv.build_inventory(db_path, project="proj_one")
    assert len(report_filtered["projects"]) == 1
    assert report_filtered["projects"][0]["project"] == "proj_one"
    assert report_filtered["totals"]["projects_count"] == 1


def test_formats_json_and_markdown(tmp_path: Path) -> None:
    db_path = tmp_path / "test_formats.sqlite3"
    _create_database(db_path)
    _insert_memory(db_path, memory_id="m1", project="proj_fmt", memory_type="workflow", title="Main Workflow")

    report = inv.build_inventory(db_path)

    # Markdown format
    md = inv.format_markdown(report)
    assert "# Инвентаризация памяти BHM" in md
    assert "proj_fmt" in md
    assert "Main Workflow" in md
    assert "Период активности" in md
    assert "Итоги по проектам" in md

    # JSON format
    js = inv.format_json(report)
    parsed = json.loads(js)
    assert parsed["schema"] == "bhm.memory-inventory.v1"
    assert parsed["database"] == "test_formats.sqlite3"
    assert parsed["totals"]["active_count"] == 1
    assert parsed["projects"][0]["project"] == "proj_fmt"
    # Ensure full database path is not leaked into the JSON output
    assert str(db_path.resolve()) not in js


def test_database_is_strictly_unchanged(tmp_path: Path) -> None:
    db_path = tmp_path / "immutable.sqlite3"
    _create_database(db_path)
    _insert_memory(db_path, memory_id="imm_1", project="alpha", memory_type="fact")

    hash_before = _file_hash(db_path)
    size_before = db_path.stat().st_size

    # Run inventory multiple times with both formats
    inv.build_inventory(db_path)
    inv.main(["--database", str(db_path), "--format", "json"])
    inv.main(["--database", str(db_path), "--format", "markdown"])

    hash_after = _file_hash(db_path)
    size_after = db_path.stat().st_size

    assert hash_before == hash_after
    assert size_before == size_after


def test_exit_code_2_on_missing_or_corrupt_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # 1. Nonexistent database
    ret_missing = inv.main(["--database", str(tmp_path / "nonexistent.sqlite3")])
    assert ret_missing == 2
    err_missing = capsys.readouterr().err
    assert "не найден" in err_missing

    # 2. Database with missing tables
    empty_db = tmp_path / "empty.sqlite3"
    with sqlite3.connect(empty_db) as conn:
        conn.execute("CREATE TABLE irrelevant (id INT)")
    ret_empty = inv.main(["--database", str(empty_db)])
    assert ret_empty == 2
    err_empty = capsys.readouterr().err
    assert "отсутствуют необходимые таблицы" in err_empty

    # 3. Database with missing required column
    bad_schema_db = tmp_path / "bad_schema.sqlite3"
    with sqlite3.connect(bad_schema_db) as conn:
        conn.execute("CREATE TABLE memories (memory_id TEXT PRIMARY KEY, project TEXT)")
        conn.execute("CREATE TABLE memory_revisions (revision_id TEXT PRIMARY KEY, memory_id TEXT)")
    ret_bad = inv.main(["--database", str(bad_schema_db)])
    assert ret_bad == 2
    err_bad = capsys.readouterr().err
    assert "отсутствуют необходимые колонки" in err_bad


def test_out_argument_writes_to_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "test_out.sqlite3"
    _create_database(db_path)
    _insert_memory(db_path, memory_id="o_1", project="proj_out", memory_type="rule")

    out_file = tmp_path / "report.md"
    ret = inv.main(["--database", str(db_path), "--out", str(out_file)])
    assert ret == 0

    stdout = capsys.readouterr().out
    assert stdout == ""  # Nothing in stdout when --out is specified
    assert out_file.is_file()
    assert "proj_out" in out_file.read_text(encoding="utf-8")


def test_recent_argument_limits_titles(tmp_path: Path) -> None:
    db_path = tmp_path / "test_recent.sqlite3"
    _create_database(db_path)
    for i in range(10):
        _insert_memory(db_path, memory_id=f"r_{i}", project="proj_rec", memory_type="log", title=f"Title {i}")

    report = inv.build_inventory(db_path, project="proj_rec", recent_limit=3)
    titles = report["projects"][0]["recent_titles"]
    assert len(titles) == 3
