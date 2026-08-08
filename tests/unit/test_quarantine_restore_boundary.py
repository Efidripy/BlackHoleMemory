from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bhm_quarantine_projection_orphans.py"


def _module():
    spec = importlib.util.spec_from_file_location("bhm_quarantine_projection_orphans_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_restore_rejects_backup_outside_manifest_directory(tmp_path: Path) -> None:
    module = _module()
    manifest_root = tmp_path / "backup"
    manifest_root.mkdir()
    outside = tmp_path / "qdrant-orphan-points.json"
    outside.write_text(json.dumps({"points": []}), encoding="utf-8")
    manifest = manifest_root / "quarantine-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": module.QUARANTINE_SCHEMA_VERSION,
                "backupPath": str(outside),
                "backupSha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.ProjectionQuarantineCliError, match="under manifest directory"):
        module._restore_from_manifest(manifest, client=object())

