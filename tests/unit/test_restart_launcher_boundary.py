from __future__ import annotations

import importlib
import threading
from pathlib import Path

import pytest

from blackholememory.filesystem_boundaries import FilesystemBoundaryError


app = importlib.import_module("blackholememory.app")


def _fixture_paths(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "repo"
    (repo_root / "scripts").mkdir(parents=True)
    (repo_root / "scripts" / "run-service.ps1").write_text("# fixture\n", encoding="utf-8")
    return repo_root, tmp_path / "runtime"


def test_restart_paths_preserve_contract_and_create_only_runtime_subdir(tmp_path: Path) -> None:
    repo_root, runtime_dir = _fixture_paths(tmp_path)

    safe_root, start_script, stdout_log, stderr_log, launcher_log = app._prepare_detached_restart_paths(
        repo_root,
        runtime_dir,
        log_suffix="fixture",
    )

    assert safe_root == repo_root
    assert start_script == repo_root / "scripts" / "run-service.ps1"
    assert stdout_log.parent == runtime_dir / "bootstrap"
    assert stderr_log.parent == runtime_dir / "bootstrap"
    assert launcher_log.parent == runtime_dir / "bootstrap"
    assert (runtime_dir / "bootstrap").is_dir()


def test_restart_paths_reject_reparse_parent_before_child_script(tmp_path: Path) -> None:
    repo_root, runtime_dir = _fixture_paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_runtime = tmp_path / "linked-runtime"
    linked_runtime.symlink_to(outside, target_is_directory=True)

    with pytest.raises(FilesystemBoundaryError):
        app._prepare_detached_restart_paths(repo_root, linked_runtime, log_suffix="fixture")


def test_restart_script_escapes_paths_as_literal_powershell_values(tmp_path: Path) -> None:
    repo_root, runtime_dir = _fixture_paths(tmp_path)
    paths = app._prepare_detached_restart_paths(repo_root, runtime_dir, log_suffix="fixture")
    script = app._build_detached_restart_script(
        repo_root=paths[0] / "operator's repo",
        start_script=paths[1],
        stdout_log=paths[2],
        stderr_log=paths[3],
        launcher_log=paths[4],
    )

    assert "operator''s repo" in script
    assert "-RedirectStandardOutput '" in script
    assert "-RedirectStandardError '" in script


def test_restart_script_preserves_authoritative_contract_without_touching_dependencies(tmp_path: Path) -> None:
    repo_root, runtime_dir = _fixture_paths(tmp_path)
    paths = app._prepare_detached_restart_paths(repo_root, runtime_dir, log_suffix="fixture")
    script = app._build_detached_restart_script(
        repo_root=paths[0],
        start_script=paths[1],
        stdout_log=paths[2],
        stderr_log=paths[3],
        launcher_log=paths[4],
    )

    assert '"-Authoritative"' in script
    assert "start-bhm-projection-sidecar.ps1" in script
    assert "start-qdrant.ps1" not in script
    assert "docker compose" not in script.lower()
    assert "lm studio" not in script.lower()


def test_restart_exit_is_delayed_until_after_launcher_handoff(monkeypatch) -> None:
    started = threading.Event()
    captured: dict[str, object] = {}

    class FakeThread:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            started.set()

    monkeypatch.setattr(app.threading, "Thread", FakeThread)
    app._schedule_process_exit(delay_seconds=0.1)

    assert started.is_set()
    assert captured["daemon"] is True
    assert captured["name"] == "bhm-restart-exit"
    assert callable(captured["target"])
