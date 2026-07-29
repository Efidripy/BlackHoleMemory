"""Validate WI-83 bounded Git-to-symbol impact evidence in a disposable repo."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from blackholememory.change_impact import build_git_symbol_impact_evidence


def _git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
        "GIT_AUTHOR_DATE": "2026-07-23T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-07-23T00:00:00Z",
    }
    result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, encoding="utf-8", env=env)
    return result.stdout.strip()


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def validate() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="bhm-wi83-") as temporary:
        root = Path(temporary)
        (root / "service.py").write_text("def route():\n    return 1\n", encoding="utf-8")
        (root / "tests.py").write_text("def test_route():\n    return route()\n", encoding="utf-8")
        _git(root, "init", "-q")
        _git(root, "add", ".")
        _git(root, "-c", "user.email=wi83@example.invalid", "-c", "user.name=WI-83", "commit", "-qm", "base")
        base = _git(root, "rev-parse", "HEAD")
        (root / "service.py").write_text("def route():\n    return 2\n", encoding="utf-8")
        _git(root, "add", "service.py")
        _git(root, "-c", "user.email=wi83@example.invalid", "-c", "user.name=WI-83", "commit", "-qm", "change")
        nodes = [
            {"node_id": "route", "stable_key": "fn:service.route", "node_kind": "function", "path": "service.py", "name": "route", "qualified_name": "service.route", "start_line": 1, "end_line": 2, "language": "python"},
            {"node_id": "test", "stable_key": "fn:tests.test_route", "node_kind": "function", "path": "tests.py", "name": "test_route", "qualified_name": "tests.test_route", "start_line": 1, "end_line": 2, "language": "python"},
        ]
        first = build_git_symbol_impact_evidence(root, ["service.py"], nodes, base_revision=base)
        second = build_git_symbol_impact_evidence(root, ["service.py"], nodes, base_revision=base)
        deterministic = first == second
        symbol_names = sorted(item["qualified_name"] for item in first["hunk_symbols"])
        checks = {
            "schema_version": first["schema_version"] == "bhm.change-impact.git-symbols.v1",
            "deterministic": deterministic,
            "hunk_symbol_correlation": "service.route" in symbol_names,
            "history_present": int(first["git_history"]["commits_considered"]) >= 2,
            "raw_source_excluded": first["provenance"]["raw_source_returned"] is False,
            "writes_worktree_false": first["execution"]["writes_worktree"] is False,
            "proposal_only": first["execution"]["authority"] == "proposal-only",
            "no_secret_or_signature_fields": all("signature" not in item for item in first["hunk_symbols"] + first["history_symbols"]),
        }
        return {
            "ok": all(checks.values()),
            "checks": checks,
            "evidence_digest": first["evidence_digest"],
            "fixture_digest": _digest({"base_revision": base, "nodes": nodes}),
            "summary": {
                "changed_paths": first["changed_paths"],
                "diff_hunks": len(first["diff_hunks"]),
                "hunk_symbols": len(first["hunk_symbols"]),
                "history_commits": first["git_history"]["commits_considered"],
                "history_symbols": len(first["history_symbols"]),
            },
            "provenance": first["provenance"],
            "execution": first["execution"],
            "rollback": "Disable the WI-83 helper or stop supplying base_revision; no database migration or worktree recovery is required.",
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = validate()
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
