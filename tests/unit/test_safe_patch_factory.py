from __future__ import annotations

import sys
from pathlib import Path

import pytest

from blackholememory.safe_patch_factory import SafePatchApprovalRequired
from blackholememory.safe_patch_factory import SafePatchFactory
from blackholememory.safe_patch_factory import SafePatchPathError


PATCH = """diff --git a/src/demo.py b/src/demo.py
--- a/src/demo.py
+++ b/src/demo.py
@@ -1,2 +1,2 @@
-VALUE = 'old'
+VALUE = 'new'
 def read():
     return VALUE
"""


def test_factory_applies_only_to_quarantine_and_collects_ast_diff_and_sandbox(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    source = repo / "src" / "demo.py"
    source.write_text("VALUE = 'old'\ndef read():\n    return VALUE\n", encoding="utf-8")
    factory = SafePatchFactory(root=tmp_path / "quarantine")

    plan = factory.prepare(task_id="safe-patch-1", repo_root=repo, allowed_files=["src/demo.py"], patch_text=PATCH)
    assert source.read_text(encoding="utf-8") == "VALUE = 'old'\ndef read():\n    return VALUE\n"
    assert (Path(plan.quarantine_root) / "candidate" / "src" / "demo.py").read_text(encoding="utf-8").startswith("VALUE = 'new'")

    ast_context = factory.ast_context(plan)
    diff = factory.diff_evidence(plan)
    sandbox = factory.run_sandbox(plan, [sys.executable, "-c", "from src.demo import read; assert read() == 'new'"])
    review = factory.review(plan, sandbox_result=sandbox, root_cause="old constant was stale")

    assert ast_context["symbol_count"] >= 1
    assert diff["changed_files"] == ["candidate/src/demo.py"] or diff["changed_files"] == ["src/demo.py"]
    assert sandbox["success"] is True
    assert review["review_status"] == "reviewable"
    assert review["apply_enabled"] is False
    assert review["commit_enabled"] is False
    assert review["root_cause_digest"]

    handoff = factory.apply_approved(plan, approval_token="operator-approved", expected_diff_digest=plan.diff_digest)
    assert handoff["approved"] is True
    assert handoff["applied"] is False
    assert handoff["committed"] is False
    assert factory.cleanup(plan.quarantine_root) is True
    assert not Path(plan.quarantine_root).exists()


def test_factory_rejects_patch_outside_allowlist_and_requires_matching_approval(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "demo.py").write_text("VALUE = 'old'\ndef read():\n    return VALUE\n", encoding="utf-8")
    factory = SafePatchFactory(root=tmp_path / "quarantine")

    with pytest.raises(SafePatchPathError):
        factory.prepare(task_id="safe-patch-2", repo_root=repo, allowed_files=["src/demo.py"], patch_text=PATCH.replace("src/demo.py", "other.py"))

    plan = factory.prepare(task_id="safe-patch-3", repo_root=repo, allowed_files=["src/demo.py"], patch_text=PATCH)
    with pytest.raises(SafePatchApprovalRequired):
        factory.apply_approved(plan, approval_token="", expected_diff_digest=plan.diff_digest)
    with pytest.raises(SafePatchApprovalRequired):
        factory.apply_approved(plan, approval_token="operator-approved", expected_diff_digest="wrong")
    factory.cleanup(plan.quarantine_root)


def test_factory_rejects_unsafe_cleanup_and_path_traversal(tmp_path: Path):
    factory = SafePatchFactory(root=tmp_path / "quarantine")
    with pytest.raises(SafePatchPathError):
        factory.cleanup(tmp_path)
    with pytest.raises(SafePatchPathError):
        factory.prepare(
            task_id="safe-patch-4",
            repo_root=tmp_path,
            allowed_files=["../outside.py"],
            patch_text=PATCH,
        )
