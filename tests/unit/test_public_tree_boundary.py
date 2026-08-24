from __future__ import annotations

from importlib.util import module_from_spec
from importlib.util import spec_from_file_location
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_validator():
    script = ROOT / "scripts" / "validate-public-tree.py"
    spec = spec_from_file_location("validate_public_tree", script)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_VALIDATOR = _load_validator()
GIT_PROBE_TIMEOUT_SECONDS = _VALIDATOR.GIT_PROBE_TIMEOUT_SECONDS
_run_git = _VALIDATOR._run_git
is_local = _VALIDATOR.is_local
load_manifest = _VALIDATOR.load_manifest


def test_manifest_declares_public_local_boundary() -> None:
    manifest = load_manifest(ROOT)
    assert "control" in manifest["public_roots"]
    assert "assets" in manifest["public_roots"]
    assert "infra" in manifest["public_roots"]
    assert ".local" in manifest["local_roots"]
    assert ".docs" in manifest["local_roots"]
    assert ".src" in manifest["local_roots"]
    assert ".workspace" in manifest["local_roots"]
    assert is_local(".local/evidence/raw.json", manifest)
    assert is_local(".docs/ops/receipt.json", manifest)
    assert not is_local("runtime/logs/session.json", manifest)
    assert is_local(".node_modules/package/index.js", manifest)
    assert is_local(".workspace/runtime/logs/session.sqlite3", manifest)
    assert is_local(".runtime-legacy/pytest-p21-1-auth/session.sqlite3", manifest)
    assert not is_local("assets/bhm-black-hole-icon.svg", manifest)
    assert not is_local("infra/qdrant/docker-compose.yml", manifest)
    assert not is_local("src/blackholememory/app.py", manifest)
    assert not is_local("docs/getting-started.md", manifest)


def test_required_identity_helper_is_public() -> None:
    manifest = load_manifest(ROOT)
    assert "control/scripts/shared/BhmObservationIdentity.ps1" in manifest["required_files"]
    assert (ROOT / "control/scripts/shared/BhmObservationIdentity.ps1").is_file()


def test_public_tree_git_probe_is_bounded_and_fails_closed(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    def timeout(*_args, **kwargs):
        calls.update(kwargs)
        raise subprocess.TimeoutExpired(kwargs.get("args", "git"), GIT_PROBE_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert _run_git(tmp_path, "status") is None
    assert calls["timeout"] == GIT_PROBE_TIMEOUT_SECONDS
