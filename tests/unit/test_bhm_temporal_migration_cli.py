from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bhm-temporal-migration.py"


def _module():
    spec = importlib.util.spec_from_file_location("bhm_temporal_migration", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_artifact_paths_stay_below_temporal_runtime_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _module()
    monkeypatch.setattr(module, "ARTIFACT_ROOT", tmp_path / "artifacts")
    assert module._artifact_path(tmp_path / "artifacts" / "plan.json", must_exist=False).name == "plan.json"
    with pytest.raises(module.TemporalMigrationError, match="artifact must stay below"):
        module._artifact_path(tmp_path / "outside.json", must_exist=False)


def test_apply_offline_guard_rejects_active_api_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(module.socket, "create_connection", lambda *_args, **_kwargs: _Connection())
    with pytest.raises(module.TemporalMigrationError, match="API listener is still active"):
        module._assert_offline_writer()


def test_apply_offline_guard_allows_refused_listener(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _module()
    monkeypatch.setattr(module, "SIDECAR_PID", tmp_path / "missing.pid")

    def refused(*_args, **_kwargs):
        raise OSError("refused")

    monkeypatch.setattr(module.socket, "create_connection", refused)
    module._assert_offline_writer()
