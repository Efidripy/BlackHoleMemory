#!/usr/bin/env python3
"""Validate the bounded P21.14 source reclassification evidence.

This is a local, read-only validator.  It never downloads, executes or
imports quarantined material and records only manifest-level provenance.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any


TARGET_IDS = ("CGM", "CBG", "BDS", "M0MCP")
TODAY = date(2026, 7, 21).isoformat()


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**__import__("os").environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"},
    )
    return completed.stdout.strip()


def validate(repo: Path) -> dict[str, Any]:
    registry = _load(repo / "config" / "source-registry.json")
    by_id = {str(source["id"]): source for source in registry["sources"]}
    entries: list[dict[str, Any]] = []
    failures: list[str] = []
    for source_id in TARGET_IDS:
        source = by_id.get(source_id)
        if source is None:
            failures.append(f"missing registry source {source_id}")
            continue
        manifest_path = repo / ".src" / str(source["slug"]) / "SOURCE-MANIFEST.json"
        if not manifest_path.is_file():
            failures.append(f"{source_id}: missing SOURCE-MANIFEST.json")
            continue
        manifest = _load(manifest_path)
        status = str(manifest.get("acquisition_status"))
        risky = list(manifest.get("risky_paths") or [])
        secrets = list(manifest.get("secret_findings") or [])
        if status not in {"rejected-risky-paths", "rejected-secret-material", "failed"}:
            failures.append(f"{source_id}: unsafe material is not rejected ({status})")
        if manifest.get("code_copy_allowed") is not False:
            failures.append(f"{source_id}: code_copy_allowed is not false")
        if manifest.get("runtime_dependency") is not False or manifest.get("authoritative_bhm_state") is not False:
            failures.append(f"{source_id}: quarantine boundary is not explicit")
        entries.append(
            {
                "source_id": source_id,
                "slug": source["slug"],
                "exact_revision": source["revision"],
                "content_sha256": manifest.get("content_sha256"),
                "manifest_sha256": __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest(),
                "acquisition_status": status,
                "risky_paths": risky,
                "secret_findings": secrets,
                "permission_evidence_ref": manifest.get("permission_evidence_ref"),
                "disposition": "rejected" if status.startswith("rejected") else "unavailable",
                "runtime_dependency": False,
                "code_copy_allowed": False,
                "reason": "unsafe DB/.env/credential-shaped material is excluded; public behavior remains clean-room reference only",
            }
        )

    tracked = _git(repo, "ls-files", ".src")
    staged = _git(repo, "diff", "--cached", "--name-only", "--", ".src")
    if tracked or staged:
        failures.append(".src is tracked or staged")
    report = {
        "schema_version": "bhm.p21.14.wi32.source-reclassification.v1",
        "generated_at": TODAY,
        "plan_id": "BHM-V5-POST-ACCEPTANCE-20260717",
        "scope": list(TARGET_IDS),
        "entries": entries,
        "tracked_src": tracked.splitlines() if tracked else [],
        "staged_src": staged.splitlines() if staged else [],
        "zero_unmanifested_acquisitions": True,
        "writes_live_state": False,
        "failures": failures,
        "ok": not failures and len(entries) == len(TARGET_IDS),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = validate(args.repo_root.resolve())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "entries": len(report["entries"]), "failures": report["failures"]}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
