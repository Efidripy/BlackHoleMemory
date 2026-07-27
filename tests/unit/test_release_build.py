from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def load_script_module(filename: str, module_name: str):
    path = REPO_ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


build_manifest = load_script_module("build-release-manifest.py", "bhm_test_build_release_manifest")
verify_release = load_script_module("verify-release-build.py", "bhm_test_verify_release_build")


def create_bundle(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "plugins/bhm-codex-connector/.codex-plugin").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "BHM_Launcher.exe").write_bytes(b"launcher")
    (root / "config/version-manifest.json").write_text(
        json.dumps({"release_version": "1.7.0"}), encoding="utf-8"
    )
    (root / "plugins/bhm-codex-connector/.codex-plugin/plugin.json").write_text(
        json.dumps({"version": "1.7.0"}), encoding="utf-8"
    )
    (root / "scripts/bhm_launcher.py").write_text("print('launcher')\n", encoding="utf-8")
    (root / "src/blackholememory").mkdir(parents=True)
    (root / "src/blackholememory/app.py").write_text("application = True\n", encoding="utf-8")
    (root / "src/blackholememory/version_manifest.py").write_text("RUNTIME_VERSION = '1.7.0'\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nversion='1.7.0'\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "LICENSE").write_text("BSD Zero Clause License\n", encoding="utf-8")


def write_manifest(root: Path) -> None:
    manifest = build_manifest.build_manifest(root, "v1.7.0")
    build_manifest.write_manifest(root, manifest)


def test_release_manifest_round_trip_and_cache_exclusion(tmp_path):
    create_bundle(tmp_path)
    cache = tmp_path / "scripts/__pycache__"
    cache.mkdir()
    (cache / "ignored.pyc").write_bytes(b"ignored")
    write_manifest(tmp_path)

    files = verify_release.verify_directory(tmp_path)
    result = verify_release.verify_mapping(files, "v1.7.0")

    assert result["ok"] is True
    assert result["manifest_file_count"] == 9
    assert "scripts/__pycache__/ignored.pyc" not in files


def test_release_verifier_detects_tampering(tmp_path):
    create_bundle(tmp_path)
    write_manifest(tmp_path)
    (tmp_path / "scripts/bhm_launcher.py").write_text("tampered\n", encoding="utf-8")

    result = verify_release.verify_mapping(verify_release.verify_directory(tmp_path), "v1.7.0")

    assert result["ok"] is False
    assert "hash mismatch: scripts/bhm_launcher.py" in result["failures"]


def test_v1_8_release_requires_root_license(tmp_path):
    create_bundle(tmp_path)
    (tmp_path / "config/version-manifest.json").write_text(
        json.dumps({"release_version": "1.8.0"}), encoding="utf-8"
    )
    (tmp_path / "plugins/bhm-codex-connector/.codex-plugin/plugin.json").write_text(
        json.dumps({"version": "1.8.0"}), encoding="utf-8"
    )
    (tmp_path / "LICENSE").unlink()
    manifest = build_manifest.build_manifest(tmp_path, "v1.8.0")
    build_manifest.write_manifest(tmp_path, manifest)

    result = verify_release.verify_mapping(verify_release.verify_directory(tmp_path), "v1.8.0")

    assert result["ok"] is False
    assert "missing required file: LICENSE" in result["failures"]


def test_archive_verifier_rejects_members_outside_release_root(tmp_path):
    create_bundle(tmp_path)
    write_manifest(tmp_path)
    archive_path = tmp_path / "release.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in tmp_path.rglob("*"):
            if path.is_file() and path != archive_path:
                relative = path.relative_to(tmp_path).as_posix()
                archive.write(path, f"BlackHoleMemory/{relative}")
        archive.writestr("outside.txt", b"unexpected")

    files, failures = verify_release.verify_archive(archive_path)

    assert "archive member outside release root: outside.txt" in failures
    assert "release-manifest.json" in files


@pytest.mark.parametrize("member", ["../escape.txt", "..\\escape.txt", "/absolute.txt", ""])
def test_archive_member_safety_rejects_escape_paths(member):
    assert verify_release.safe_member(member) is False
