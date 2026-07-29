from __future__ import annotations

from pathlib import Path

from scripts.validate_public_tree import is_local, load_manifest


ROOT = Path(__file__).resolve().parents[2]


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
