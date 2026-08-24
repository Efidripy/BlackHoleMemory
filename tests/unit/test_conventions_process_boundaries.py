from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load():
    path = ROOT / "scripts" / "validate-bhm-conventions.py"
    spec = importlib.util.spec_from_file_location("validate_bhm_wi04_conventions", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wi04_child_process_is_bounded(monkeypatch) -> None:
    module = _load()
    calls: dict[str, object] = {}

    def timeout(*_args, **kwargs):
        calls.update(kwargs)
        raise subprocess.TimeoutExpired(kwargs.get("args", "python"), module.WI04_PROCESS_TIMEOUT_SECONDS)

    monkeypatch.setattr(module.subprocess, "run", timeout)
    try:
        module._run_bounded_child(["python", "-V"], cwd=ROOT)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("WI-04 child process must retain bounded timeout semantics")
    assert calls["timeout"] == module.WI04_PROCESS_TIMEOUT_SECONDS


def test_wi04_validator_tracks_current_mcp_catalog() -> None:
    module = _load()
    from blackholememory.mcp_surfaces import CORE_TOOL_NAMES

    assert len(CORE_TOOL_NAMES) == module.WI04_EXPECTED_CORE_TOOL_COUNT == 35
