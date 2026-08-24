from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wi01_benchmark_git_fixture_process_is_bounded(monkeypatch, tmp_path: Path) -> None:
    module = _load("benchmark_bhm_repository_index", "scripts/benchmark-bhm-repository-index.py")
    calls: dict[str, object] = {}

    def timeout(*_args, **kwargs):
        calls.update(kwargs)
        raise subprocess.TimeoutExpired(kwargs.get("args", "git"), module.WI01_PROCESS_TIMEOUT_SECONDS)

    monkeypatch.setattr(module.subprocess, "run", timeout)
    try:
        module._git(tmp_path, "status")
    except RuntimeError as exc:
        assert str(exc) == "WI-01 Git fixture process unavailable"
    else:
        raise AssertionError("WI-01 benchmark Git process must fail closed")
    assert calls["timeout"] == module.WI01_PROCESS_TIMEOUT_SECONDS


def test_wi01_validator_child_process_is_bounded(monkeypatch) -> None:
    module = _load("validate_bhm_repository_index", "scripts/validate-bhm-repository-index.py")
    calls: dict[str, object] = {}

    def timeout(*_args, **kwargs):
        calls.update(kwargs)
        raise subprocess.TimeoutExpired(kwargs.get("args", "python"), module.WI01_PROCESS_TIMEOUT_SECONDS)

    monkeypatch.setattr(module.subprocess, "run", timeout)
    try:
        module._run_bounded(["python", "-V"], check=False)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("WI-01 child process must retain bounded timeout semantics")
    assert calls["timeout"] == module.WI01_PROCESS_TIMEOUT_SECONDS
