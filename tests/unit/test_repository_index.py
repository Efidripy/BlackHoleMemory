from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from blackholememory.repository_index import RepositoryIndexInjectedFailure
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import RepositoryWatcher
from blackholememory.repository_index import SQLiteRepositoryIndexStore
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state
from blackholememory.repository_index import RepositoryRootError
from blackholememory.repository_index import verify_repository_snapshot


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / "assets").mkdir()
    (root / "src" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (root / "src" / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text("from src.a import a\n\ndef test_a():\n    assert a() == 1\n", encoding="utf-8")
    (root / "docs" / "readme.md").write_text("# Demo\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=synthetic\n", encoding="utf-8")
    (root / "src" / "bundle.min.js").write_text("const generated=true;\n", encoding="utf-8")
    (root / "assets" / "binary.txt").write_bytes(b"text\x00binary")
    fake_token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    (root / "assets" / "credential.txt").write_text(f"token={fake_token}\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "add", "-f", ".")
    _git(root, "commit", "-m", "fixture")
    return root


def _source() -> RepositorySourceProvenance:
    return RepositorySourceProvenance(
        source_url="https://example.invalid/fixture.git",
        license="MIT fixture",
        evidence_class="E0",
        owner="fixture",
        source_registry_id="FIXTURE",
    )


def test_cold_index_is_deterministic_bounded_and_deduplicated(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "index.sqlite3"

    first = index_repository(root, database, project="demo", source=_source())
    second = index_repository(root, database, project="demo", source=_source())

    assert first["ok"] is True
    assert first["status"] == "completed"
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["metrics"]["deduplicated"] is True
    assert first["gates"]["raw_source_persisted"] is False
    store = SQLiteRepositoryIndexStore(database)
    snapshot = store.snapshot(first["snapshot_id"], include_files=True)
    assert verify_repository_snapshot(snapshot) is True
    paths = {item["path"] for item in snapshot["files"]}
    assert paths == {"docs/readme.md", "src/a.py", "src/b.py", "tests/test_a.py"}
    reasons = {item["path"]: item["reason"] for item in snapshot["skips"]}
    assert reasons[".env"] == "secret-path"
    assert reasons["src/bundle.min.js"] == "generated"
    assert reasons["assets/binary.txt"] == "binary"
    assert reasons["assets/credential.txt"] == "secret-content"
    assert snapshot["source"]["source_registry_id"] == "FIXTURE"

    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(repository_index_snapshot_files)").fetchall()
        }
    assert "content" not in columns
    assert "content_sha256" in columns


def test_force_refresh_publishes_new_epoch_without_source_or_qdrant_writes(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "index.sqlite3"

    first = index_repository(root, database, project="demo", source=_source())
    refreshed = index_repository(root, database, project="demo", source=_source(), force_refresh=True)

    assert first["ok"] is True
    assert refreshed["ok"] is True
    assert refreshed["snapshot_id"] != first["snapshot_id"]
    assert refreshed["metrics"]["force_refresh"] is True
    assert refreshed["metrics"]["deduplicated"] is False
    assert refreshed["gates"]["raw_source_persisted"] is False
    assert refreshed["gates"]["qdrant_written"] is False
    snapshot = SQLiteRepositoryIndexStore(database).snapshot(refreshed["snapshot_id"], include_files=True)
    assert verify_repository_snapshot(snapshot) is True
    assert snapshot["source"]["refresh_nonce"].startswith("operator-refresh-")


def test_deduplicated_completed_job_repairs_stale_current_pointer(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "index.sqlite3"

    first = index_repository(root, database, project="demo", source=_source())
    (root / "src" / "a.py").write_text("def a():\n    return 11\n", encoding="utf-8")
    second = index_repository(root, database, project="demo", source=_source())
    assert second["snapshot_id"] != first["snapshot_id"]

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE repository_index_current SET snapshot_id = ? WHERE project = ? AND root_id = ?",
            (first["snapshot_id"], "demo", second["root_id"]),
        )
        connection.commit()

    repaired = index_repository(root, database, project="demo", source=_source())

    assert repaired["snapshot_id"] == second["snapshot_id"]
    assert repaired["metrics"]["deduplicated"] is False
    assert repaired["metrics"]["pointer_repaired"] is True
    assert SQLiteRepositoryIndexStore(database).current_snapshot("demo", second["root_id"])["snapshot_id"] == second["snapshot_id"]


def test_cbm_metadata_only_language_inventory_suffixes_are_indexed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    suffixes = {
        ".vue": "vue",
        ".svelte": "svelte",
        ".astro": "astro",
        ".beancount": "beancount",
        ".sol": "solidity",
        ".zig": "zig",
        ".nim": "nim",
        ".jl": "julia",
        ".clj": "clojure",
        ".cljs": "clojure",
        ".groovy": "groovy",
        ".m": "objective-c",
        ".mm": "objective-c",
        ".asm": "assembly",
        ".v": "verilog",
        ".vhd": "vhdl",
    }
    for index, suffix in enumerate(suffixes, start=1):
        (root / f"fixture_{index}{suffix}").write_text("metadata-only fixture\n", encoding="utf-8")
    database = tmp_path / "index.sqlite3"
    indexed = index_repository(root, database, project="demo", source=_source())
    snapshot = SQLiteRepositoryIndexStore(database).snapshot(indexed["snapshot_id"], include_files=True)
    languages = {item["language"] for item in snapshot["files"]}
    assert languages == set(suffixes.values())


def test_incremental_index_reports_change_remove_add_and_unique_rename(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "index.sqlite3"
    first = index_repository(root, database, project="demo", source=_source())

    (root / "src" / "a.py").write_text("def a():\n    return 10\n", encoding="utf-8")
    (root / "src" / "b.py").rename(root / "src" / "renamed.py")
    (root / "docs" / "readme.md").unlink()
    (root / "src" / "new.py").write_text("VALUE = 3\n", encoding="utf-8")
    second = index_repository(root, database, project="demo", source=_source())

    assert second["snapshot_id"] != first["snapshot_id"]
    assert second["metrics"]["reused_unchanged_files"] >= 1
    delta = second["snapshot"]["delta"]
    assert delta["changed"] == ["src/a.py"]
    assert delta["added"] == ["src/new.py"]
    assert delta["removed"] == ["docs/readme.md"]
    assert delta["renamed"] == [
        {
            "from": "src/b.py",
            "to": "src/renamed.py",
            "content_sha256": delta["renamed"][0]["content_sha256"],
        }
    ]
    assert second["snapshot"]["previous_snapshot_id"] == first["snapshot_id"]


def test_job_resumes_after_bounded_stop_and_matches_full_snapshot(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "resume.sqlite3"
    partial = index_repository(
        root,
        database,
        project="demo",
        source=_source(),
        max_files_per_run=2,
    )
    resumed = index_repository(root, database, project="demo", source=_source())
    full = index_repository(root, tmp_path / "full.sqlite3", project="demo", source=_source())

    assert partial["status"] == "running"
    assert partial["progress"]["processed_candidates"] == 2
    assert resumed["status"] == "completed"
    assert resumed["metrics"]["resumed"] is True
    assert resumed["snapshot"]["snapshot_digest"] == full["snapshot"]["snapshot_digest"]


def test_probe_rejects_reparse_and_hardlink_candidates_before_resolve(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "main.py"
    source.write_text("print('ok')\n", encoding="utf-8")
    symlink = root / "inside-link.py"
    try:
        symlink.symlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    hard_source = root / "hard-source.py"
    hard_source.write_text("print('hard')\n", encoding="utf-8")
    hardlink = root / "hard.py"
    try:
        hardlink.hardlink_to(hard_source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    state = probe_repository_state(root, project="filesystem-boundary")
    candidate_paths = {item.path for item in state.candidates}
    skip_reasons = {item.path: item.reason for item in state.prefiltered_skips}
    assert "main.py" in candidate_paths
    assert "inside-link.py" not in candidate_paths
    assert "hard.py" not in candidate_paths
    assert skip_reasons["inside-link.py"] == "filesystem-boundary"
    assert skip_reasons["hard.py"] == "filesystem-boundary"


def test_probe_rejects_reparse_repository_root_before_resolution(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(RepositoryRootError, match="filesystem boundary"):
        probe_repository_state(linked_root, project="filesystem-boundary")


def test_failure_before_publish_preserves_last_known_good_then_recovers(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "index.sqlite3"
    first = index_repository(root, database, project="demo", source=_source())
    state = probe_repository_state(root, project="demo")
    store = SQLiteRepositoryIndexStore(database)

    (root / "src" / "a.py").write_text("def a():\n    return 99\n", encoding="utf-8")
    with pytest.raises(RepositoryIndexInjectedFailure):
        index_repository(
            root,
            database,
            project="demo",
            source=_source(),
            fail_before_publish=True,
        )
    assert store.current_snapshot("demo", state.root_id)["snapshot_id"] == first["snapshot_id"]

    recovered = index_repository(root, database, project="demo", source=_source())
    assert recovered["status"] == "completed"
    assert recovered["snapshot_id"] != first["snapshot_id"]


def test_polling_watcher_is_explicit_and_detects_freshness(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "index.sqlite3"
    watcher = RepositoryWatcher(root, database, project="demo", source=_source())

    assert watcher.poll()["changed"] is True
    result = watcher.run(cycles=1, interval_seconds=0)
    assert result["ok"] is True
    assert result["starts_background_daemon"] is False
    assert result["resumed_from_checkpoint"] is False
    checkpoint = SQLiteRepositoryIndexStore(database).get_watch_checkpoint("demo", watcher.poll()["state"]["root_id"])
    assert checkpoint is not None
    assert checkpoint["status"] == "completed"
    SQLiteRepositoryIndexStore(database).save_watch_checkpoint(
        "demo",
        watcher.poll()["state"]["root_id"],
        {"status": "running", "cycle": 1, "state_digest": checkpoint["state_digest"]},
    )
    resumed = watcher.run(cycles=1, interval_seconds=0)
    assert resumed["resumed_from_checkpoint"] is True
    assert watcher.poll()["changed"] is False

    (root / "src" / "a.py").write_text("def a():\n    return 5\n", encoding="utf-8")
    assert watcher.poll()["changed"] is True


def test_polling_watcher_debounce_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
    database = tmp_path / "runtime" / "index.sqlite3"
    watcher = RepositoryWatcher(root, database, project="demo", source=_source())
    result = watcher.run(cycles=1, interval_seconds=0, debounce_seconds=0)
    assert result["debounce_seconds"] == 0
    with pytest.raises(ValueError):
        watcher.run(cycles=1, debounce_seconds=31)


def test_watcher_backpressure_blocks_a_different_running_job_without_indexing(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "runtime" / "index.sqlite3"
    source = _source()
    watcher = RepositoryWatcher(root, database, project="demo", source=source)

    state = probe_repository_state(root, project="demo")
    store = SQLiteRepositoryIndexStore(database)
    running = store.begin_or_resume_job(state, source)
    assert running["status"] == "running"

    (root / "src" / "a.py").write_text("def a():\n    return 9\n", encoding="utf-8")
    pressure = watcher.backpressure()
    assert pressure["active_job_count"] == 1
    assert pressure["blocking_job_count"] == 1
    assert pressure["blocked"] is True
    assert pressure["operator_managed"] is True
    assert pressure["autonomous_apply"] is False
    assert pressure["writes_sqlite_state"] is False

    result = watcher.run(cycles=1, interval_seconds=0)
    event = result["events"][0]
    assert result["ok"] is True
    assert result["backpressured_cycles"] == 1
    assert event["backpressured"] is True
    assert event["requires_operator_action"] is True
    assert event["index"] is None
    assert store.current_snapshot("demo", state.root_id) is None
    assert store.job(running["job_id"])["status"] == "running"


def test_watcher_backpressure_allows_same_state_crash_resume(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "runtime" / "index.sqlite3"
    source = _source()
    watcher = RepositoryWatcher(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    running = SQLiteRepositoryIndexStore(database).begin_or_resume_job(state, source)

    pressure = watcher.backpressure()
    assert pressure["active_job_count"] == 1
    assert pressure["blocking_job_count"] == 0
    assert pressure["blocked"] is False
    result = watcher.run(cycles=1, interval_seconds=0)
    assert result["backpressured_cycles"] == 0
    assert result["events"][0]["index"]["job_id"] == running["job_id"]


def test_watcher_backpressure_is_bounded_and_read_only_when_database_is_absent(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "runtime" / "missing.sqlite3"
    watcher = RepositoryWatcher(root, database, project="demo")
    assert watcher.backpressure()["active_job_count"] == 0
    assert not database.exists()
    with pytest.raises(ValueError):
        RepositoryWatcher(root, database, project="demo", max_inflight_jobs=5)
