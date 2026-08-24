from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load():
    path = ROOT / "scripts" / "validate-bhm-code-graph-query.py"
    spec = importlib.util.spec_from_file_location("validate_bhm_wi03_code_graph_query", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wi03_child_process_is_bounded(monkeypatch) -> None:
    module = _load()
    calls: dict[str, object] = {}

    def timeout(*_args, **kwargs):
        calls.update(kwargs)
        raise subprocess.TimeoutExpired(kwargs.get("args", "python"), module.WI03_PROCESS_TIMEOUT_SECONDS)

    monkeypatch.setattr(module.subprocess, "run", timeout)
    try:
        module._run_bounded_child(["python", "-V"], cwd=ROOT)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("WI-03 child process must retain bounded timeout semantics")
    assert calls["timeout"] == module.WI03_PROCESS_TIMEOUT_SECONDS


def test_wi03_operation_fixture_covers_allowlist() -> None:
    module = _load()
    from blackholememory.code_graph_query import ALLOWED_OPERATIONS

    assert set(module.WI03_OPERATION_QUERIES) == set(ALLOWED_OPERATIONS)
