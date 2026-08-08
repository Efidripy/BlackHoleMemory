from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "apply-bhm-permission-attestation.py"


def _module():
    spec = importlib.util.spec_from_file_location("bhm_permission_attestation_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(slug: str = "fixture-source") -> dict[str, object]:
    return {
        "id": "fixture.source",
        "slug": slug,
        "name": "fixture/source",
        "source_url": "https://example.invalid/fixture.git",
        "revision": "0123456789abcdef0123456789abcdef01234567",
        "allowed_use": "tests",
        "attribution": "fixture",
        "purpose": ["test fixture"],
        "recheck_date": "2099-01-01",
    }


def test_permission_attestation_uses_boundary_aware_json_writers() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "replace_bytes_safely" in source
    assert ".write_text(" not in source
    assert ".write_bytes(" not in source


def test_compile_ledger_tracks_current_registry_size() -> None:
    module = _module()
    registry = {"sources": [_source()]}
    ledger = module.compile_ledger(registry)
    assert len(ledger["entries"]) == 1
    assert ledger["entries"][0]["source_id"] == "fixture.source"


def test_apply_metadata_replaces_registry_and_manifest_safely(tmp_path: Path) -> None:
    module = _module()
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / ".src" / "fixture-source").mkdir(parents=True)
    (repo / "docs" / "ops").mkdir(parents=True)
    source = _source()
    registry = {"sources": [source]}
    (repo / "config" / "source-registry.json").write_text(json.dumps(registry), encoding="utf-8")
    (repo / ".src" / "fixture-source" / "SOURCE-MANIFEST.json").write_text(
        json.dumps({"slug": source["slug"], "code_copy_allowed": False}), encoding="utf-8"
    )

    module.apply_metadata(registry, module.compile_ledger(registry), repo)

    updated_registry = json.loads((repo / "config" / "source-registry.json").read_text(encoding="utf-8"))
    updated_manifest = json.loads(
        (repo / ".src" / "fixture-source" / "SOURCE-MANIFEST.json").read_text(encoding="utf-8")
    )
    assert updated_registry["sources"][0]["permission_status"] == "written-permission"
    assert updated_manifest["permission_status"] == "written-permission"
    assert updated_manifest["code_copy_allowed"] is False


def test_apply_metadata_rejects_unsafe_source_slug_before_writing(tmp_path: Path) -> None:
    module = _module()
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / ".src").mkdir()
    source = _source("../outside")
    registry = {"sources": [source]}
    ledger = module.compile_ledger(registry)

    with pytest.raises(ValueError, match="unsafe source slug"):
        module.apply_metadata(registry, ledger, repo)
    assert not (repo / "config" / "source-registry.json").exists()


def test_apply_metadata_rejects_hardlinked_registry(tmp_path: Path) -> None:
    module = _module()
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / ".src" / "fixture-source").mkdir(parents=True)
    source = _source()
    registry = {"sources": [source]}
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    registry_path = repo / "config" / "source-registry.json"
    try:
        registry_path.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        module.apply_metadata(registry, module.compile_ledger(registry), repo)
    assert outside.read_text(encoding="utf-8") == "sentinel"
