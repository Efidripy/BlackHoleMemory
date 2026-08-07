from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-bhm-wi14-migration.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("validate_bhm_wi14_migration", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wi14_child_process_uses_bounded_timeout(monkeypatch) -> None:
    module = _load_module()
    calls: dict[str, object] = {}

    class Result:
        returncode = 0

    def fake_run(*args, **kwargs):
        calls.update(kwargs)
        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result, timed_out = module._run_child(["fixture"], cwd=module.ROOT, env={})

    assert result is not None
    assert result.returncode == 0
    assert timed_out is False
    assert calls["timeout"] == module.CHILD_PROCESS_TIMEOUT_SECONDS


def test_wi14_child_process_fails_closed_on_timeout(monkeypatch) -> None:
    module = _load_module()

    def timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(kwargs.get("args", "child"), module.CHILD_PROCESS_TIMEOUT_SECONDS)

    monkeypatch.setattr(module.subprocess, "run", timeout)
    result, timed_out = module._run_child(["fixture"], cwd=module.ROOT, env={})

    assert result is None
    assert timed_out is True
