#!/usr/bin/env python3
"""WI-00 source/license/quarantine exit gate."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from blackholememory.resource_limits import PROCESS_EXECUTION_GIT_PROBE_TIMEOUT_SECONDS
from blackholememory.source_registry import SourceRegistryError, load_registry, verify_registry


ROOT = Path(__file__).resolve().parents[1]
GIT_PROBE_TIMEOUT_SECONDS = PROCESS_EXECUTION_GIT_PROBE_TIMEOUT_SECONDS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_lines(*args: str) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_PROBE_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        return [f"<git-error:{completed.stderr.strip()}>"]
    return [line for line in completed.stdout.splitlines() if line.strip()]


def main() -> int:
    args = parse_args()
    registry_path = ROOT / "config" / "source-registry.json"
    source_root = ROOT / ".src"
    integration_path = ROOT / "config" / "cbm-integration.json"
    failures: list[str] = []
    try:
        registry = load_registry(registry_path)
        validation = verify_registry(registry_path, source_root)
        failures.extend(validation["failures"])
        integration = json.loads(integration_path.read_text(encoding="utf-8"))
        if integration.get("schema_version") != "bhm.cbm.integration.v1":
            failures.append("integration feature-flag schema mismatch")
        flags = integration.get("feature_flags", {})
        enabled = sorted(name for name, value in flags.items() if value is not False)
        if enabled:
            failures.append(f"WI-00 requires all integration feature flags off: {enabled}")
        tracked = _git_lines("ls-files", "--", ".src")
        staged = _git_lines("diff", "--cached", "--name-only", "--", ".src")
        if tracked:
            failures.append(f".src tracked paths present: {tracked[:5]}")
        if staged:
            failures.append(f".src staged paths present: {staged[:5]}")
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        release_builder = (ROOT / "scripts" / "build-release.ps1").read_text(encoding="utf-8")
        if ".src/" not in gitignore:
            failures.append("root .gitignore does not exclude .src/")
        if ".src/" not in dockerignore:
            failures.append("root .dockerignore does not exclude .src/")
        if "verify-local-source-boundary.ps1" not in release_builder:
            failures.append("release builder does not invoke local source boundary validator")
        restricted_native = [
            source["id"]
            for source in registry["sources"]
            if source["license_status"] in {"unknown", "unverified", "proprietary", "copyleft", "source-available"}
            and source["disposition"] not in {"reference-only", "rejected"}
        ]
        if restricted_native:
            failures.append(f"restricted sources are not reference-only/rejected: {restricted_native}")
        report = {
            "schema_version": "bhm.wi00.source-passport.v1",
            "ok": not failures,
            "plan_id": registry.get("plan_id"),
            "source_count": validation["source_count"],
            "acquired_count": validation["acquired_count"],
            "failed_reference_count": validation["failed_reference_count"],
            "rejected_count": validation["rejected_count"],
            "dispositions": validation["dispositions"],
            "permission_status_counts": validation["permission_status_counts"],
            "permission_migration_pending_count": validation["permission_migration_pending_count"],
            "registry_sha256": validation["registry_sha256"],
            "integration_sha256": _sha256(integration_path),
            "license_file_count": validation["license_file_count"],
            "dependency_manifest_count": validation["dependency_manifest_count"],
            "security_document_count": validation["security_document_count"],
            "quarantine_file_count": sum(int(item["file_count"]) for item in validation["source_results"]),
            "quarantine_source_bytes": sum(int(item["source_bytes"]) for item in validation["source_results"]),
            "tracked_src_count": len(tracked),
            "staged_src_count": len(staged),
            "feature_flags_enabled": enabled,
            "restricted_native": restricted_native,
            "source_results": validation["source_results"],
            "failures": failures,
            "writes_live_state": False,
        }
    except (OSError, ValueError, SourceRegistryError, json.JSONDecodeError) as exc:
        report = {"schema_version": "bhm.wi00.source-passport.v1", "ok": False, "failures": [str(exc)], "writes_live_state": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        replace_bytes_safely(args.report, rendered.encode("utf-8"))
    print(rendered, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
