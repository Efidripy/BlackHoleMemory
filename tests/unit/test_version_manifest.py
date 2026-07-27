from __future__ import annotations

# The unit module adds the repository's src directory before imports.
# ruff: noqa: E402

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.version_manifest import BROKER_VERSION
from blackholememory.version_manifest import PACKAGE_VERSION
from blackholememory.version_manifest import PLUGIN_VERSION
from blackholememory.version_manifest import RUNTIME_VERSION
from blackholememory.version_manifest import UI_VERSION
from blackholememory.version_manifest import VERSION_MANIFEST
from blackholememory.version_manifest import load_version_manifest


def test_version_manifest_exposes_one_release_version_for_all_surfaces():
    release = VERSION_MANIFEST.release_version

    assert PACKAGE_VERSION == release
    assert PLUGIN_VERSION == release
    assert release in RUNTIME_VERSION
    assert release in BROKER_VERSION
    assert release in UI_VERSION


def test_version_manifest_rejects_component_drift(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    payload = VERSION_MANIFEST.as_dict()
    payload["schema_version"] = 1
    payload["components"]["plugin"] = "9.9.9"
    (config_dir / "version-manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="package and plugin versions"):
        load_version_manifest(tmp_path)
