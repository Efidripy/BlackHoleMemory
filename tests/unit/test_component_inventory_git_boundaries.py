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


def test_validator_git_probe_is_bounded_and_fail_closed(monkeypatch, tmp_path: Path) -> None:
    module = _load(
        "validate_bhm_p28_wi68_component_inventory",
        "scripts/validate-bhm-component-inventory.py",
    )
    calls: dict[str, object] = {}

    def timeout(*_args, **kwargs):
        calls.update(kwargs)
        raise subprocess.TimeoutExpired(kwargs.get("args", "git"), module.GIT_PROBE_TIMEOUT_SECONDS)

    monkeypatch.setattr(module.subprocess, "run", timeout)

    try:
        module.git_files(tmp_path)
    except module.GitInventoryError as exc:
        assert str(exc) == "git source inventory probe unavailable"
    else:
        raise AssertionError("Git probe must fail closed on timeout")
    assert calls["timeout"] == module.GIT_PROBE_TIMEOUT_SECONDS


def test_rebuild_git_probe_is_bounded_and_fail_closed(monkeypatch) -> None:
    module = _load("rebuild_bhm_component_inventory", "scripts/rebuild-bhm-component-inventory.py")
    calls: dict[str, object] = {}

    def timeout(*_args, **kwargs):
        calls.update(kwargs)
        raise subprocess.TimeoutExpired(kwargs.get("args", "git"), module.GIT_PROBE_TIMEOUT_SECONDS)

    monkeypatch.setattr(module.subprocess, "run", timeout)

    try:
        module.tracked_paths()
    except module.GitInventoryError as exc:
        assert str(exc) == "git source inventory probe unavailable"
    else:
        raise AssertionError("Git probe must fail closed on timeout")
    assert calls["timeout"] == module.GIT_PROBE_TIMEOUT_SECONDS
