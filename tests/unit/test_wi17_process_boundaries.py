from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load():
    path = ROOT / "scripts" / "validate-bhm-final-acceptance.py"
    spec = importlib.util.spec_from_file_location("validate_bhm_wi17_final_acceptance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wi17_child_process_is_bounded_and_fail_closed(monkeypatch) -> None:
    module = _load()
    calls: dict[str, object] = {}

    def timeout(*_args, **kwargs):
        calls.update(kwargs)
        raise subprocess.TimeoutExpired(kwargs.get("args", "python"), module.WI17_PROCESS_TIMEOUT_SECONDS)

    monkeypatch.setattr(module.subprocess, "run", timeout)
    ok, output = module._run(["python", "-V"], env={})

    assert ok is False
    assert "timed out" in output.lower()
    assert calls["timeout"] == module.WI17_PROCESS_TIMEOUT_SECONDS
