from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "bhm-local-artifact-cleanup.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("bhm_local_artifact_cleanup", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _policy(root: Path) -> Path:
    policy = {
        "schemaVersion": "bhm.local-artifact-retention-policy.v1",
        "managedRoots": [".runtime"],
        "protectedRoots": [".runtime/live-memory", ".runtime/backups"],
        "rules": [
            {"id": "empty-scratch", "parent": ".runtime", "glob": "pytest-*", "kind": "directory", "emptyOnly": True, "minAgeDays": 7},
            {"id": "old-log", "parent": ".runtime", "glob": "*.log", "kind": "file", "minAgeDays": 7},
        ],
    }
    path = root / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    return path


def _age(path: Path, timestamp: float) -> None:
    os.utime(path, (timestamp, timestamp))


def test_plan_is_read_only_and_selects_only_expired_empty_scratch(tmp_path: Path):
    module = _load_module()
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    stale = runtime / "pytest-stale"
    stale.mkdir()
    nonempty = runtime / "pytest-nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("evidence", encoding="utf-8")
    protected = runtime / "live-memory"
    protected.mkdir()
    old = 1_700_000_000
    _age(stale, old)
    _age(nonempty, old)
    _age(protected, old)

    plan = module.build_plan(tmp_path, _policy(tmp_path), as_of="2024-01-01T00:00:00Z")

    assert [row["path"] for row in plan["candidates"]] == [".runtime/pytest-stale"]
    assert stale.exists()
    assert nonempty.exists()
    assert protected.exists()


def test_apply_requires_exact_plan_and_removes_no_other_path(tmp_path: Path):
    module = _load_module()
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    stale = runtime / "pytest-stale"
    stale.mkdir()
    old = 1_700_000_000
    _age(stale, old)
    policy = _policy(tmp_path)
    as_of = "2024-01-01T00:00:00Z"
    plan = module.build_plan(tmp_path, policy, as_of=as_of)

    with pytest.raises(module.ArtifactCleanupError, match="digest"):
        module.apply_plan(tmp_path, policy, as_of=as_of, expected_digest="wrong")
    assert stale.exists()

    result = module.apply_plan(tmp_path, policy, as_of=as_of, expected_digest=plan["plan_digest"])
    assert result["applied"] is True
    assert not stale.exists()


def test_policy_rejects_parent_outside_managed_roots(tmp_path: Path):
    module = _load_module()
    (tmp_path / ".runtime").mkdir()
    policy = _policy(tmp_path)
    payload = json.loads(policy.read_text(encoding="utf-8"))
    payload["rules"][0]["parent"] = "."
    policy.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(module.ArtifactCleanupError, match="managed roots"):
        module.build_plan(tmp_path, policy, as_of="2024-01-01T00:00:00Z")


def test_inaccessible_candidate_is_reported_and_blocks_apply(monkeypatch, tmp_path: Path):
    module = _load_module()
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    stale = runtime / "pytest-stale"
    stale.mkdir()
    _age(stale, 1_700_000_000)
    policy = _policy(tmp_path)

    def denied(*args, **kwargs):
        raise PermissionError("denied by fixture")

    monkeypatch.setattr(module, "_candidate_from_path", denied)
    plan = module.build_plan(tmp_path, policy, as_of="2024-01-01T00:00:00Z")

    assert plan["candidates"] == []
    assert plan["blocked"] == [{"rule_id": "empty-scratch", "path": ".runtime/pytest-stale", "reason": "denied by fixture"}]
    with pytest.raises(module.ArtifactCleanupError, match="inaccessible"):
        module.apply_plan(tmp_path, policy, as_of="2024-01-01T00:00:00Z", expected_digest=plan["plan_digest"])
