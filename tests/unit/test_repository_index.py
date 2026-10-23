from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

import pytest

from blackholememory import repository_index as repository_index_module
from blackholememory.code_graph import build_code_graph
from blackholememory.filesystem_boundaries import FilesystemBoundaryError
from blackholememory.repository_index import (
    STALE_JOB_DISPOSITION_REASON,
    RepositoryIndexError,
    RepositoryIndexInjectedFailure,
    RepositoryRootError,
    RepositorySourceProvenance,
    RepositoryWatcher,
    SQLiteRepositoryIndexStore,
    index_repository,
    probe_repository_state,
    repository_root_id,
    verify_repository_snapshot,
)


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


def test_repository_root_id_matches_probe_without_enumeration(monkeypatch, tmp_path: Path) -> None:
    root = _repository(tmp_path)
    expected = probe_repository_state(root, project="demo").root_id

    def fail_enumeration(_root: Path):
        raise AssertionError("repository enumeration must not run")

    monkeypatch.setattr(repository_index_module, "_enumerate_repository_paths", fail_enumeration)
    assert repository_root_id(root) == expected


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


def test_source_index_keeps_path_only_literals_but_skips_credentials(tmp_path: Path) -> None:
    root = tmp_path / "source-paths"
    root.mkdir()
    (root / "app.js").write_text(
        "import './client.js';\nfetch('/api/items');\n",
        encoding="utf-8",
    )
    (root / "client.js").write_text("export const ok = true;\n", encoding="utf-8")
    (root / "credentials.txt").write_text(
        "token=ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n",
        encoding="utf-8",
    )

    indexed = index_repository(root, tmp_path / "paths.sqlite3", project="paths", source=_source())
    snapshot = SQLiteRepositoryIndexStore(tmp_path / "paths.sqlite3").snapshot(indexed["snapshot_id"], include_files=True)

    assert {item["path"] for item in snapshot["files"]} == {"app.js", "client.js"}
    assert {item["path"]: item["reason"] for item in snapshot["skips"]}["credentials.txt"] == "secret-content"


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


def test_non_force_index_keeps_newest_force_refresh_epoch_current(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "index.sqlite3"

    first = index_repository(root, database, project="demo", source=_source())
    refreshed = index_repository(root, database, project="demo", source=_source(), force_refresh=True)
    repeated = index_repository(root, database, project="demo", source=_source())

    assert refreshed["snapshot_id"] != first["snapshot_id"]
    assert repeated["snapshot_id"] == refreshed["snapshot_id"]
    assert repeated["job_id"] == refreshed["job_id"]
    assert repeated["metrics"]["deduplicated"] is True
    assert repeated["metrics"]["pointer_repaired"] is False
    assert SQLiteRepositoryIndexStore(database).current_snapshot("demo", repeated["root_id"])["snapshot_id"] == refreshed["snapshot_id"]


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


def test_repository_watcher_rejects_reparse_root_before_resolution(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(FilesystemBoundaryError, match="symlink|junction|reparse"):
        RepositoryWatcher(linked_root, tmp_path / "index.sqlite3")


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


def _copy_sqlite_database(source: Path, target: Path) -> str:
    with sqlite3.connect(source) as source_connection, sqlite3.connect(target) as target_connection:
        source_connection.backup(target_connection)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _stale_disposition_fixture(tmp_path: Path) -> tuple[Path, Path, SQLiteRepositoryIndexStore, object, list[str]]:
    root = _repository(tmp_path)
    database = tmp_path / "stale-disposition.sqlite3"
    completed = index_repository(root, database, project="demo", source=_source())
    prior_state = probe_repository_state(root, project="demo")
    build_code_graph(
        database,
        project="demo",
        root_id=prior_state.root_id,
        repository_snapshot_id=completed["snapshot_id"],
    )
    store = SQLiteRepositoryIndexStore(database)
    first = store.begin_or_resume_job(prior_state, _source(), force_refresh_nonce="stale-disposition-a")
    second = store.begin_or_resume_job(prior_state, _source(), force_refresh_nonce="stale-disposition-b")
    (root / "src" / "a.py").write_text("def a():\n    return 99\n", encoding="utf-8")
    _git(root, "add", "src/a.py")
    _git(root, "commit", "-m", "new binding")
    current_state = probe_repository_state(root, project="demo")
    return root, database, store, current_state, [first["job_id"], second["job_id"]]


def test_stale_job_disposition_plans_and_applies_exact_zero_progress_rows(tmp_path: Path) -> None:
    _, database, store, state, job_ids = _stale_disposition_fixture(tmp_path)
    before_index_pointer = store.current_snapshot("demo", state.root_id)["snapshot_id"]
    with sqlite3.connect(database) as connection:
        before_graph_pointer = connection.execute(
            "SELECT graph_snapshot_id FROM repository_code_graph_current WHERE project = ? AND root_id = ?",
            ("demo", state.root_id),
        ).fetchone()[0]
        candidates_before = connection.execute(
            "SELECT COUNT(*) FROM repository_index_job_candidates WHERE job_id IN (?, ?)",
            tuple(job_ids),
        ).fetchone()[0]
    plan = store.plan_stale_job_disposition(state=state, job_ids=job_ids, operator="fixture-operator")
    assert plan["reason"] == STALE_JOB_DISPOSITION_REASON
    assert [item["job_id"] for item in plan["jobs"]] == sorted(job_ids)
    backup = tmp_path / "rollback.sqlite3"
    backup_sha256 = _copy_sqlite_database(database, backup)

    applied = store.apply_stale_job_disposition(
        state=state,
        plan=plan,
        expected_plan_digest=plan["plan_digest"],
        offline=True,
        backup_path=backup,
        backup_sha256=backup_sha256,
    )

    assert applied["ok"] is True
    assert applied["updates_current_pointers"] is False
    assert applied["deletes_job_candidates"] is False
    assert {store.job(job_id)["status"] for job_id in job_ids} == {"failed"}
    assert store.current_snapshot("demo", state.root_id)["snapshot_id"] == before_index_pointer
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT graph_snapshot_id FROM repository_code_graph_current WHERE project = ? AND root_id = ?",
            ("demo", state.root_id),
        ).fetchone()[0] == before_graph_pointer
        assert connection.execute(
            "SELECT COUNT(*) FROM repository_index_job_candidates WHERE job_id IN (?, ?)",
            tuple(job_ids),
        ).fetchone()[0] == candidates_before


def test_stale_job_disposition_rejects_wrong_id_non_running_and_binding_drift(tmp_path: Path) -> None:
    _, database, store, state, job_ids = _stale_disposition_fixture(tmp_path)
    with pytest.raises(RepositoryIndexError, match="job not found"):
        store.plan_stale_job_disposition(state=state, job_ids=["missing-job"], operator="fixture-operator")
    plan = store.plan_stale_job_disposition(state=state, job_ids=job_ids, operator="fixture-operator")
    backup = tmp_path / "rollback.sqlite3"
    backup_sha256 = _copy_sqlite_database(database, backup)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE repository_index_jobs SET state_digest = ? WHERE job_id = ?",
            ("changed-binding", job_ids[0]),
        )
        connection.commit()
    with pytest.raises(RepositoryIndexError, match="plan drifted"):
        store.apply_stale_job_disposition(
            state=state,
            plan=plan,
            expected_plan_digest=plan["plan_digest"],
            offline=True,
            backup_path=backup,
            backup_sha256=backup_sha256,
        )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE repository_index_jobs SET status = 'failed' WHERE job_id = ?",
            (job_ids[1],),
        )
        connection.commit()
    with pytest.raises(RepositoryIndexError, match="job is not writable"):
        store.save_batch(job_ids[1], [], [], cursor_path=None)
    with pytest.raises(RepositoryIndexError, match="not running"):
        store.plan_stale_job_disposition(state=state, job_ids=job_ids, operator="fixture-operator")


def test_stale_job_disposition_rejects_pointer_drift_invalid_backup_and_missing_offline_gate(tmp_path: Path) -> None:
    _, database, store, state, job_ids = _stale_disposition_fixture(tmp_path)
    plan = store.plan_stale_job_disposition(state=state, job_ids=job_ids, operator="fixture-operator")
    backup = tmp_path / "rollback.sqlite3"
    backup_sha256 = _copy_sqlite_database(database, backup)
    with pytest.raises(ValueError, match="offline boundary"):
        store.apply_stale_job_disposition(
            state=state,
            plan=plan,
            expected_plan_digest=plan["plan_digest"],
            offline=False,
            backup_path=backup,
            backup_sha256=backup_sha256,
        )
    with pytest.raises(RepositoryIndexError, match="backup SHA-256"):
        store.apply_stale_job_disposition(
            state=state,
            plan=plan,
            expected_plan_digest=plan["plan_digest"],
            offline=True,
            backup_path=backup,
            backup_sha256="0" * 64,
        )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE repository_index_current SET updated_at = ? WHERE project = ? AND root_id = ?",
            ("pointer-drift", "demo", state.root_id),
        )
        connection.commit()
    with pytest.raises(RepositoryIndexError, match="plan drifted"):
        store.apply_stale_job_disposition(
            state=state,
            plan=plan,
            expected_plan_digest=plan["plan_digest"],
            offline=True,
            backup_path=backup,
            backup_sha256=backup_sha256,
        )


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


def test_polling_watcher_retries_transient_index_failure_and_recovers(monkeypatch, tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "index.sqlite3"
    watcher = RepositoryWatcher(
        root,
        database,
        project="demo",
        source=_source(),
        retry_attempts=2,
        retry_backoff_seconds=0,
    )
    original = repository_index_module.index_repository
    calls = 0

    def flaky(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository_index_module, "index_repository", flaky)
    result = watcher.run(cycles=1, interval_seconds=0)

    assert result["ok"] is True
    assert calls == 2
    assert result["events"][0]["retry"] == {
        "attempts": 2,
        "max_attempts": 2,
        "recovered": True,
    }
    checkpoint = SQLiteRepositoryIndexStore(database).get_watch_checkpoint(
        "demo", result["events"][0]["poll"]["state"]["root_id"]
    )
    assert checkpoint["status"] == "completed"


def test_polling_watcher_persists_retryable_failure_checkpoint(monkeypatch, tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "index.sqlite3"
    watcher = RepositoryWatcher(
        root,
        database,
        project="demo",
        retry_attempts=2,
        retry_backoff_seconds=0,
    )

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repository_index_module, "index_repository", unavailable)
    result = watcher.run(cycles=1, interval_seconds=0)
    event = result["events"][0]
    checkpoint = SQLiteRepositoryIndexStore(database).get_watch_checkpoint(
        "demo", event["poll"]["state"]["root_id"]
    )

    assert result["ok"] is False
    assert event["index"] is None
    assert event["error"]["retryable"] is True
    assert event["retry"]["attempts"] == 2
    assert checkpoint["status"] == "failed"
    assert checkpoint["retryable"] is True
    assert checkpoint["error_code"] == "OperationalError"
    assert checkpoint["attempts"] == 2


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
