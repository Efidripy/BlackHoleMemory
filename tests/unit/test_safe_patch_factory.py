from __future__ import annotations

from dataclasses import replace
import os
import sys
from pathlib import Path

import pytest

import blackholememory.safe_patch_factory as safe_patch_factory
from blackholememory.safe_patch_factory import SafePatchApprovalRequired
from blackholememory.safe_patch_factory import SafePatchError
from blackholememory.safe_patch_factory import SafePatchFactory
from blackholememory.safe_patch_factory import SafePatchPathError
from blackholememory.resource_limits import PROCESS_EXECUTION_SAFE_PATCH_CLEANUP_TIMEOUT_SECONDS


PATCH = """diff --git a/src/demo.py b/src/demo.py
--- a/src/demo.py
+++ b/src/demo.py
@@ -1,2 +1,2 @@
-VALUE = 'old'
+VALUE = 'new'
 def read():
     return VALUE
"""


def test_safe_patch_process_cleanup_uses_registry_timeout() -> None:
    source = Path(safe_patch_factory.__file__).read_text(encoding="utf-8")
    assert "PROCESS_EXECUTION_SAFE_PATCH_CLEANUP_TIMEOUT_SECONDS" in source
    assert "communicate(timeout=2.0)" not in source
    assert "timeout=2.0" not in source
    assert PROCESS_EXECUTION_SAFE_PATCH_CLEANUP_TIMEOUT_SECONDS == 2.0


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
    sandbox = factory.run_sandbox(
        plan,
        [sys.executable, "-c", "from src.demo import read; assert read() == 'new'"],
        allow_host_process=True,
    )
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


def test_factory_requires_explicit_host_process_authorization(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "demo.py").write_text("VALUE = 'old'\ndef read():\n    return VALUE\n", encoding="utf-8")
    factory = SafePatchFactory(root=tmp_path / "quarantine")
    plan = factory.prepare(task_id="safe-patch-explicit-host", repo_root=repo, allowed_files=["src/demo.py"], patch_text=PATCH)

    with pytest.raises(SafePatchError, match="explicit allow_host_process"):
        factory.run_sandbox(plan, [sys.executable, "-c", "pass"])
    factory.cleanup(plan.quarantine_root)


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


def test_factory_rejects_reparse_cleanup_target_without_following_it(tmp_path: Path) -> None:
    factory = SafePatchFactory(root=tmp_path / "quarantine")
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    link = factory.root / "linked-quarantine"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(SafePatchPathError, match="reparse point"):
        factory.cleanup(link)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_factory_rejects_reparse_source_before_copy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 'outside'\n", encoding="utf-8")
    source_link = repo / "src" / "demo.py"
    try:
        source_link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    factory = SafePatchFactory(root=tmp_path / "quarantine")
    with pytest.raises(SafePatchPathError, match="reparse point"):
        factory.prepare(
            task_id="safe-patch-reparse-source",
            repo_root=repo,
            allowed_files=["src/demo.py"],
            patch_text=PATCH,
        )
    assert outside.read_text(encoding="utf-8") == "VALUE = 'outside'\n"


@pytest.mark.parametrize("path", [r"..\outside.py", r"C:\outside.py", r"\\server\share\outside.py"])
def test_factory_rejects_portable_unsafe_allowlist_paths(tmp_path: Path, path: str) -> None:
    factory = SafePatchFactory(root=tmp_path / "quarantine")

    with pytest.raises(SafePatchPathError):
        factory.prepare(
            task_id="safe-patch-portable-path",
            repo_root=tmp_path,
            allowed_files=[path],
            patch_text=PATCH,
        )


def test_factory_revalidates_plan_quarantine_before_sandbox_execution(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "demo.py").write_text("VALUE = 'old'\ndef read():\n    return VALUE\n", encoding="utf-8")
    factory = SafePatchFactory(root=tmp_path / "quarantine")
    plan = factory.prepare(
        task_id="safe-patch-forged-plan",
        repo_root=repo,
        allowed_files=["src/demo.py"],
        patch_text=PATCH,
    )
    forged = replace(plan, quarantine_root=str(tmp_path))
    with pytest.raises(SafePatchPathError):
        factory.run_sandbox(forged, [sys.executable, "-c", "print('never runs')"], allow_host_process=True)
    factory.cleanup(plan.quarantine_root)


def test_factory_sandbox_does_not_inherit_secret_environment(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "demo.py").write_text("VALUE = 'old'\ndef read():\n    return VALUE\n", encoding="utf-8")
    factory = SafePatchFactory(root=tmp_path / "quarantine")
    plan = factory.prepare(
        task_id="safe-patch-env-boundary",
        repo_root=repo,
        allowed_files=["src/demo.py"],
        patch_text=PATCH,
    )
    monkeypatch.setenv("BHM_TEST_SECRET", "must-not-cross-boundary")
    sandbox = factory.run_sandbox(
        plan,
        [sys.executable, "-c", "import os; print(os.getenv('BHM_TEST_SECRET', 'missing'))"],
        allow_host_process=True,
    )
    assert sandbox["success"] is True
    assert sandbox["stdout"].strip() == "missing"
    assert sandbox["execution_boundary"]["environment"] == "allowlisted"
    assert sandbox["execution_boundary"]["network_isolated"] is False
    factory.cleanup(plan.quarantine_root)


def test_factory_sandbox_rejects_import_path_override(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "demo.py").write_text("VALUE = 'old'\ndef read():\n    return VALUE\n", encoding="utf-8")
    factory = SafePatchFactory(root=tmp_path / "quarantine")
    plan = factory.prepare(
        task_id="safe-patch-env-override",
        repo_root=repo,
        allowed_files=["src/demo.py"],
        patch_text=PATCH,
    )
    with pytest.raises(SafePatchPathError):
        factory.run_sandbox(
            plan,
            [sys.executable, "-c", "pass"],
            allow_host_process=True,
            env={"PYTHONPATH": os.getcwd()},
        )
    factory.cleanup(plan.quarantine_root)


def test_factory_sandbox_timeout_reports_process_group_cleanup(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "demo.py").write_text("VALUE = 'old'\ndef read():\n    return VALUE\n", encoding="utf-8")
    factory = SafePatchFactory(root=tmp_path / "quarantine")
    plan = factory.prepare(
        task_id="safe-patch-timeout-boundary",
        repo_root=repo,
        allowed_files=["src/demo.py"],
        patch_text=PATCH,
    )
    sandbox = factory.run_sandbox(
        plan,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        allow_host_process=True,
        timeout_seconds=0.1,
    )
    assert sandbox["success"] is False
    assert sandbox["timed_out"] is True
    assert sandbox["process_group_terminated"] is True
    factory.cleanup(plan.quarantine_root)


def test_factory_rejects_tampered_candidate_file_before_execution(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "demo.py").write_text("VALUE = 'old'\ndef read():\n    return VALUE\n", encoding="utf-8")
    factory = SafePatchFactory(root=tmp_path / "quarantine")
    plan = factory.prepare(
        task_id="safe-patch-tampered-candidate",
        repo_root=repo,
        allowed_files=["src/demo.py"],
        patch_text=PATCH,
    )
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 'outside'\n", encoding="utf-8")
    candidate_file = Path(plan.quarantine_root) / "candidate" / "src" / "demo.py"
    candidate_file.unlink()
    try:
        candidate_file.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    with pytest.raises(SafePatchPathError, match="reparse point"):
        factory.run_sandbox(plan, [sys.executable, "-c", "print('never runs')"], allow_host_process=True)
    factory.cleanup(plan.quarantine_root)
