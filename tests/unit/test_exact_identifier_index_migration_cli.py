from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[2] / "scripts" / "bhm-exact-identifier-index.py"
    spec = importlib.util.spec_from_file_location("bhm_exact_identifier_index", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_proof_rejects_live_api_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(module.socket, "create_connection", lambda *_args, **_kwargs: Connection())

    with pytest.raises(module.ExactIdentifierIndexMigrationError, match="listener is still active"):
        module._assert_offline()


def test_offline_proof_rejects_active_projection_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    monkeypatch.setattr(module.socket, "create_connection", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))

    class Process:
        pid = 91
        info = {"cmdline": ["powershell", "run-bhm-projection-sidecar.ps1"]}

    monkeypatch.setattr(module.psutil, "process_iter", lambda *_args, **_kwargs: [Process()])

    with pytest.raises(module.ExactIdentifierIndexMigrationError, match="projection sidecar is still active"):
        module._assert_offline()
