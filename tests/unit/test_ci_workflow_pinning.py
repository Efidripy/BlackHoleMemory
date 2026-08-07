from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-ci-workflow-pinning.py"


def _module():
    spec = importlib.util.spec_from_file_location("validate_ci_workflow_pinning", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checked_in_workflows_use_immutable_action_shas():
    report = _module().validate(REPO_ROOT)
    assert report["ok"] is True
    assert report["actions_checked"] == 4
    assert report["failures"] == []


def test_mutable_action_reference_fails_closed(tmp_path: Path):
    workflow_root = tmp_path / ".github" / "workflows"
    workflow_root.mkdir(parents=True)
    (workflow_root / "ci.yml").write_text(
        "name: test\njobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n",
        encoding="utf-8",
    )

    report = _module().validate(tmp_path)
    assert report["ok"] is False
    assert report["failures"][0]["reason"] == "mutable_ref"
