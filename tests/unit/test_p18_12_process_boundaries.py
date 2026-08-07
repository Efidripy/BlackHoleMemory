from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load():
    path = ROOT / "scripts" / "validate-bhm-p18.12-adapter-generation.py"
    spec = importlib.util.spec_from_file_location("validate_bhm_p18_12_adapter_generation", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_p18_12_child_process_is_bounded_and_fail_closed(monkeypatch) -> None:
    module = _load()
    calls: dict[str, object] = {}

    def timeout(*_args, **kwargs):
        calls.update(kwargs)
        raise subprocess.TimeoutExpired(kwargs.get("args", "python"), module.P18_12_PROCESS_TIMEOUT_SECONDS)

    monkeypatch.setattr(module.subprocess, "run", timeout)
    result = module._run("--manifest", "fixture.json", "--canary")

    assert result["ok"] is False
    assert result["returncode"] is None
    assert "timed out" in result["error"].lower()
    assert calls["timeout"] == module.P18_12_PROCESS_TIMEOUT_SECONDS
