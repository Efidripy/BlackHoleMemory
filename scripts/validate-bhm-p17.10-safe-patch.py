"""Deterministic offline gate for the P17.10 Safe Patch Factory."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from blackholememory.safe_patch_factory import SafePatchFactory


PATCH = """diff --git a/src/demo.py b/src/demo.py
--- a/src/demo.py
+++ b/src/demo.py
@@ -1,2 +1,2 @@
-VALUE = 'old'
+VALUE = 'new'
 def read():
     return VALUE
"""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bhm-p17.10-") as directory:
        root = Path(directory)
        repo = root / "repo"
        source = repo / "src" / "demo.py"
        source.parent.mkdir(parents=True)
        original = "VALUE = 'old'\ndef read():\n    return VALUE\n"
        source.write_text(original, encoding="utf-8")
        factory = SafePatchFactory(root=root / "quarantine")
        plan = factory.prepare(task_id="p17.10-validator", repo_root=repo, allowed_files=["src/demo.py"], patch_text=PATCH)
        sandbox = factory.run_sandbox(plan, [sys.executable, "-c", "from src.demo import read; assert read() == 'new'"])
        review = factory.review(plan, sandbox_result=sandbox, root_cause="stale constant")
        handoff = factory.apply_approved(plan, approval_token="operator-approved", expected_diff_digest=plan.diff_digest)
        checks = {
            "source_untouched": source.read_text(encoding="utf-8") == original,
            "sandbox_green": sandbox["success"] is True,
            "ast_bounded": review["ast_context"]["bounded"] is True,
            "reviewable": review["review_status"] == "reviewable",
            "apply_is_handoff_only": handoff["approved"] is True and handoff["applied"] is False and handoff["committed"] is False,
            "proposal_only": review["auto_apply"] is False and review["commit_enabled"] is False,
        }
        cleanup = factory.cleanup(plan.quarantine_root)
        checks["cleanup"] = cleanup is True and not Path(plan.quarantine_root).exists()
    report = {
        "ok": all(checks.values()),
        "schema_version": plan.as_dict()["schema_version"],
        "plan_id": plan.plan_id,
        "diff_digest": plan.diff_digest,
        "sandbox": {"success": sandbox["success"], "exit_code": sandbox["exit_code"]},
        "review_status": review["review_status"],
        "checks": checks,
        "apply_enabled": False,
        "commit_enabled": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
