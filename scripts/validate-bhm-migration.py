"""Deterministic WI-14 migration/compatibility exit validator."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from blackholememory.resource_limits import PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS
from blackholememory import app as bhm_app
from blackholememory.migration_compatibility import build_migration_preview
from blackholememory.migration_compatibility import compute_source_hash
from blackholememory.migration_compatibility import verify_migration_digest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "bhm-migration.py"
BENCHMARK = ROOT / "scripts" / "benchmark-bhm-migration.py"
CHILD_PROCESS_TIMEOUT_SECONDS = PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS


def _run_child(command: list[str], *, cwd: Path, env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str] | None, bool]:
    """Run a fixture-only child validator with a finite fail-closed bound."""

    try:
        return (
            subprocess.run(
                command,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=CHILD_PROCESS_TIMEOUT_SECONDS,
            ),
            False,
        )
    except subprocess.TimeoutExpired:
        return None, True


def _fixture() -> list[dict]:
    accepted = {"id": "accepted-1", "project": "fixture", "content": "SQLite remains authoritative.", "source_ref": "https://example.invalid/source", "commit": "abc123", "license": "MIT", "reviewed": True, "reviewer": "operator", "confidence": 0.9, "author": "upstream"}
    accepted["source_hash"] = compute_source_hash(accepted)
    quarantine = {"id": "quarantine-1", "project": "fixture", "content": "Needs human review.", "source_ref": "https://example.invalid/source", "commit": "abc123", "license": "MIT", "reviewed": False}
    rejected = {"id": "rejected-1", "project": "fixture", "content": "Hash drift.", "source_ref": "https://example.invalid/source", "commit": "abc123", "license": "MIT", "reviewed": True, "reviewer": "operator", "source_hash": "bad"}
    conflict = {"id": "conflict-1", "project": "fixture", "content": "SQLite remains authoritative.", "source_ref": "https://example.invalid/other", "commit": "def456", "license": "MIT", "reviewed": False}
    duplicate = dict(accepted)
    return [accepted, quarantine, rejected, conflict, duplicate]


def _hidden_api() -> bool:
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    route = routes.get("/bhm/migration/preview")
    return route is not None and route.include_in_schema is False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    args = parser.parse_args()
    records = _fixture()
    preview = build_migration_preview(records, source_kind="fixture", source_url="https://example.invalid/source", source_commit="abc123", source_license="MIT", input_schema="generic.v1", reviewer="operator", project="fixture")
    checks = {
        "schema_digest": preview["schema_version"] == "bhm.migration-compatibility.v1" and verify_migration_digest(preview),
        "accepted_quarantine_reject_conflict": preview["counts"]["accepted"] == 1 and preview["counts"]["quarantined"] >= 2 and preview["counts"]["rejected"] == 1 and preview["counts"]["conflicted"] == 1,
        "duplicate_source_hash": preview["counts"]["duplicate"] == 1,
        "source_hash_gate": any("source_hash_mismatch" in row["reasons"] for row in preview["staging_rows"]),
        "license_and_provenance": preview["checks"]["source_provenance_complete"] and preview["checks"]["license_gate"],
        "unknown_fields_preserved": preview["compatibility"]["unknown_fields_preserved"] and preview["compatibility"]["silent_field_drop"] is False,
        "raw_content_not_emitted": preview["security"]["raw_content_emitted"] is False and all("content" not in row or "sha256" in row["content"] for row in preview["staging_rows"]),
        "rollback_passport": preview["checks"]["rollback_passport"] and preview["rollback"]["apply_performed"] is False,
        "no_authority_writes": preview["checks"]["no_authority_writes"] and all(value is False for value in preview["execution"].values() if isinstance(value, bool)),
        "hidden_api": _hidden_api(),
        "cli_smoke": False,
        "benchmark": False,
    }
    with tempfile.TemporaryDirectory(prefix="bhm-wi14-validator-") as raw:
        temp = Path(raw)
        fixture_path = temp / "fixture.json"
        fixture_path.write_text(json.dumps({"records": records, "source_kind": "fixture", "source_url": "https://example.invalid/source", "source_commit": "abc123", "source_license": "MIT", "input_schema": "generic.v1", "reviewer": "operator", "project": "fixture"}, ensure_ascii=False), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        cli_report = temp / "cli.json"
        cli, cli_timed_out = _run_child([sys.executable, str(CLI), "--fixture", str(fixture_path), "--report", str(cli_report)], cwd=ROOT, env=env)
        cli_payload = json.loads(cli_report.read_text(encoding="utf-8")) if cli_report.exists() else {}
        checks["cli_smoke"] = cli is not None and cli.returncode == 0 and cli_payload.get("migration_digest") == preview.get("migration_digest")
        benchmark_report = temp / "benchmark.json"
        benchmark, benchmark_timed_out = _run_child([sys.executable, str(BENCHMARK), "--items", "24", "--iterations", "16", "--p95-budget-ms", "250", "--report", str(benchmark_report)], cwd=ROOT, env=env)
        benchmark_payload = json.loads(benchmark_report.read_text(encoding="utf-8")) if benchmark_report.exists() else {}
        checks["benchmark"] = benchmark is not None and benchmark.returncode == 0 and benchmark_payload.get("ok") is True
        details = {"migration_digest": preview["migration_digest"], "counts": preview["counts"], "benchmark": benchmark_payload.get("latency", {}), "timed_out": {"cli": cli_timed_out, "benchmark": benchmark_timed_out}}
    failed = [name for name, value in checks.items() if not value]
    report = {"schema_version": "bhm.wi14.migration-validation.v1", "ok": not failed, "check_count": len(checks), "passed_count": len(checks) - len(failed), "checks": checks, "failed": failed, "details": details, "writes_live_state": False, "apply_performed": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
