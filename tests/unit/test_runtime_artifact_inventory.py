from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "bhm-runtime-artifact-inventory.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("bhm_runtime_artifact_inventory", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _policy(root: Path) -> Path:
    payload = {
        "schemaVersion": "bhm.runtime-artifact-governance.v1",
        "rules": [
            {"id": "live", "path": ".runtime/live", "owner": "runtime", "class": "authority", "disposition": "protected", "reason": "active"},
            {"id": "history", "path": ".runtime/history", "owner": "audit", "class": "receipt", "disposition": "archive-review", "minimumRetentionDays": 30, "reason": "history"},
        ],
    }
    path = root / "policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_inventory_is_read_only_and_reports_protection_and_review_date(tmp_path: Path):
    module = _load_module()
    live = tmp_path / ".runtime" / "live"
    history = tmp_path / ".runtime" / "history"
    live.mkdir(parents=True)
    history.mkdir(parents=True)
    (live / "memories.sqlite3").write_bytes(b"authority")
    receipt = history / "receipt.json"
    receipt.write_bytes(b"receipt")
    os.utime(history, (1_700_000_000, 1_700_000_000))
    os.utime(receipt, (1_700_000_000, 1_700_000_000))

    report = module.build_inventory(tmp_path, _policy(tmp_path), as_of="2023-11-30T00:00:00Z")

    assert [item["state"] for item in report["items"]] == ["protected", "retain-until-review"]
    assert report["items"][0]["bytes"] == len(b"authority")
    assert report["items"][1]["review_after"] == "2023-12-14T22:13:20Z"
    assert report["summary"]["reported_item_bytes_may_overlap"] is True
    assert (live / "memories.sqlite3").exists()
    assert (history / "receipt.json").exists()


def test_inventory_rejects_escape_path(tmp_path: Path):
    module = _load_module()
    policy = _policy(tmp_path)
    payload = json.loads(policy.read_text(encoding="utf-8"))
    payload["rules"][0]["path"] = "../outside"
    policy.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(module.RuntimeArtifactInventoryError, match="unsafe policy path"):
        module.build_inventory(tmp_path, policy, as_of="2024-01-01T00:00:00Z")


def test_inventory_ignores_transient_sqlite_sidecar_disappearance(monkeypatch, tmp_path: Path):
    module = _load_module()
    live = tmp_path / ".runtime" / "live"
    live.mkdir(parents=True)
    stable = live / "memories.sqlite3"
    transient = live / "memories.sqlite3-shm"
    stable.write_bytes(b"authority")
    transient.write_bytes(b"sidecar")
    original_reparse = module._reparse

    def disappear_before_stat(path: Path) -> bool:
        if path == transient and path.exists():
            path.unlink()
        return original_reparse(path)

    monkeypatch.setattr(module, "_reparse", disappear_before_stat)

    report = module.build_inventory(tmp_path, _policy(tmp_path), as_of="2024-01-01T00:00:00Z")

    assert report["items"][0]["state"] == "protected"
    assert report["items"][0]["bytes"] == len(b"authority")
    assert stable.exists()


def test_checked_in_policy_preserves_active_authority_roots():
    payload = json.loads((REPO_ROOT / "config" / "runtime-artifact-governance.json").read_text(encoding="utf-8"))
    rules = {rule["id"]: rule for rule in payload["rules"]}

    assert rules["authoritative-sqlite"]["disposition"] == "protected"
    assert rules["qdrant-projection"]["disposition"] == "protected"
    assert rules["wl174-validation"]["disposition"] == "archive-review"
