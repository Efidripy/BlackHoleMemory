from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WI00 = _load("validate_bhm_wi00_source_passport", "validate-bhm-wi00-source-passport.py")
P21 = _load("validate_bhm_p21_18_source_freeze", "validate-bhm-p21.18-source-freeze.py")
P2114 = _load("validate_bhm_p21_14_source_reclassification", "validate-bhm-p21.14-source-reclassification.py")
P231 = _load("validate_bhm_p23_1_small_repo", "validate-bhm-p23.1-small-repo.py")
P2883 = _load("validate_bhm_p28_wi83_git_impact", "validate-bhm-p28-wi83-git-impact.py")


def test_wi00_git_probe_is_bounded(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = "one\ntwo\n"
        stderr = ""

    def fake_run(*_args, **kwargs):
        calls.update(kwargs)
        return Result()

    monkeypatch.setattr(WI00.subprocess, "run", fake_run)
    assert WI00._git_lines("status") == ["one", "two"]
    assert calls["timeout"] == WI00.GIT_PROBE_TIMEOUT_SECONDS


def test_source_freeze_flags_reject_only_source_mutation_or_training() -> None:
    safe_operational_flags = {"integration_enabled": True, "code_index_enabled": True}
    unsafe_flags = {**safe_operational_flags, "source_import_enabled": True, "training_enabled": True}

    assert WI00._unsafe_source_flags(safe_operational_flags) == []
    assert P21._unsafe_source_flags(safe_operational_flags) == []
    assert WI00._unsafe_source_flags(unsafe_flags) == ["source_import_enabled", "training_enabled"]
    assert P21._unsafe_source_flags(unsafe_flags) == ["source_import_enabled", "training_enabled"]


def test_p21_source_freeze_git_probe_is_bounded(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class Result:
        stdout = "one\n"

    def fake_run(*_args, **kwargs):
        calls.update(kwargs)
        return Result()

    monkeypatch.setattr(P21.subprocess, "run", fake_run)
    assert P21.git("status") == ["one"]
    assert calls["timeout"] == P21.GIT_PROBE_TIMEOUT_SECONDS


def test_p21_source_freeze_timeout_remains_fail_closed(monkeypatch) -> None:
    def timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(kwargs.get("args", "git"), P21.GIT_PROBE_TIMEOUT_SECONDS)

    monkeypatch.setattr(P21.subprocess, "run", timeout)
    try:
        P21.git("status")
    except subprocess.TimeoutExpired:
        pass
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("source-freeze validator swallowed its bounded Git timeout")


def test_p2114_source_reclassification_git_probe_is_bounded(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = "one\n"
        stderr = ""

    def fake_run(*_args, **kwargs):
        calls.update(kwargs)
        return Result()

    monkeypatch.setattr(P2114.subprocess, "run", fake_run)
    assert P2114._git(tmp_path, "status") == "one"
    assert calls["timeout"] == P2114.GIT_PROBE_TIMEOUT_SECONDS


def test_p231_small_repo_git_probe_is_bounded(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    class Result:
        stdout = "one\n"

    def fake_run(*_args, **kwargs):
        calls.update(kwargs)
        return Result()

    monkeypatch.setattr(P231.subprocess, "run", fake_run)
    assert P231._git(tmp_path, "status") == "one"
    assert calls["timeout"] == P231.GIT_PROBE_TIMEOUT_SECONDS


def test_p2883_git_impact_probe_is_bounded(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    class Result:
        stdout = "one\n"

    def fake_run(*_args, **kwargs):
        calls.update(kwargs)
        return Result()

    monkeypatch.setattr(P2883.subprocess, "run", fake_run)
    assert P2883._git(tmp_path, "status") == "one"
    assert calls["timeout"] == P2883.GIT_PROBE_TIMEOUT_SECONDS
