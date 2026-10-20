from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import bhm_launcher as launcher  # noqa: E402


def test_find_state_root_uses_repo_for_frozen_project(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / "src" / "blackholememory").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "bundled_resource_root", lambda: tmp_path / "bundle")
    monkeypatch.setattr(launcher, "find_project_root", lambda: project)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert launcher.find_state_root() == project


def test_find_state_root_uses_localappdata_without_repo(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(launcher, "bundled_resource_root", lambda: tmp_path / "bundle")
    monkeypatch.setattr(launcher, "find_project_root", lambda: tmp_path / "not-a-project")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert launcher.find_state_root() == tmp_path / "local" / "BlackHoleMemory"


def test_sidecar_status_requires_matching_process_identity(monkeypatch, tmp_path: Path) -> None:
    pid_path, _ = launcher._sidecar_paths(tmp_path)
    pid_path.parent.mkdir(parents=True)
    started_at = datetime.now(timezone.utc).timestamp()
    pid_path.write_text(
        json.dumps({"pid": 123, "started_at": started_at, "role": launcher.SIDECAR_ROLE}),
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher.psutil, "Process", lambda _pid: SimpleNamespace(create_time=lambda: started_at))
    assert launcher.sidecar_status(tmp_path)["alive"] is True
    monkeypatch.setattr(launcher.psutil, "Process", lambda _pid: SimpleNamespace(create_time=lambda: started_at + 10))
    assert launcher.sidecar_status(tmp_path)["alive"] is False


def test_sidecar_status_without_pid_file_is_offline(tmp_path: Path) -> None:
    assert launcher.sidecar_status(tmp_path)["alive"] is False


def test_stop_sidecar_creates_stop_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(launcher, "sidecar_status", lambda _root: {"alive": False})
    result = launcher.stop_sidecar(tmp_path, timeout=0)
    assert result["ok"] is True
    assert (tmp_path / ".runtime" / "bootstrap" / launcher.SIDECAR_STOP_FILENAME).exists()


def test_start_sidecar_does_not_start_second_instance(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(launcher, "sidecar_status", lambda _root: {"alive": True, "pid": 42})
    monkeypatch.setattr(launcher, "run_detached", lambda args, cwd: calls.append(args))
    result = launcher.start_sidecar(tmp_path)
    assert result["started"] is False
    assert calls == []
