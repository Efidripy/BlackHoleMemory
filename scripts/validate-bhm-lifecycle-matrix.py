#!/usr/bin/env python3
"""Validate the bounded BHM startup/shutdown and partial-start lifecycle matrix.

The validator is read-only with respect to authoritative BHM stores.  It
checks that the canonical lifespan still contains the fail-closed startup
guards and finally-block cleanup calls, then runs the disposable unit matrix
that exercises successful shutdown, worker-start failure, failed background
work, and worker-stop failure.  No live runtime restart, MCP repair, SQLite
write, projection drain, or Qdrant mutation is performed.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys
import os
from typing import Any

from blackholememory.filesystem_boundaries import replace_bytes_safely


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "src" / "blackholememory" / "app.py"
TEST_PATH = ROOT / "tests" / "unit" / "test_app_lifecycle.py"
SCHEMA_VERSION = "bhm.lifecycle-matrix-validation.v1"
EXPECTED_TESTS = (
    "test_lifespan_shutdown_stops_workers_and_infra_after_context_exit",
    "test_lifespan_cleans_background_tasks_when_worker_startup_fails",
    "test_lifespan_observes_failed_background_task_and_cleans_siblings",
    "test_lifespan_cleanup_runs_when_worker_stop_fails",
    "test_lifespan_rejects_non_loopback_listener_before_runtime_start",
)
REQUIRED_LIFESPAN_MARKERS = (
    "validate_loopback_listener_host",
    "caller_auth_configuration_error",
    "_memory_store_state",
    "_wait_for_required_storage_ready",
    "_stop_hook_queue_workers",
    "_cancel_lifespan_task",
    "_cleanup_registered_infra_processes",
)


def _write_report(path: Path, rendered: str) -> None:
    replace_bytes_safely(path, (rendered + "\n").encode("utf-8"))


def _function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _lifespan_source(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_app_lifespan":
            lines = path.read_text(encoding="utf-8").splitlines()
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise ValueError("_app_lifespan_not_found")


def _run_disposable_tests(timeout_seconds: float) -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", str(TEST_PATH.relative_to(ROOT)), "-q"]
    try:
        env = os.environ.copy()
        src_path = str(ROOT / "src")
        env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": None,
            "timeout": True,
            "passed": 0,
            "output_tail": "pytest_timeout",
        }
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    match = re.search(r"(?P<passed>\d+) passed", output)
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "timeout": False,
        "passed": int(match.group("passed")) if match else 0,
        "output_tail": output[-2000:],
    }


def validate(*, run_tests: bool = True, timeout_seconds: float = 120.0) -> dict[str, Any]:
    app_source = APP_PATH.read_text(encoding="utf-8")
    lifespan_source = _lifespan_source(APP_PATH)
    test_names = _function_names(TEST_PATH)
    missing_tests = sorted(set(EXPECTED_TESTS) - test_names)
    missing_markers = sorted(marker for marker in REQUIRED_LIFESPAN_MARKERS if marker not in lifespan_source)
    tests = _run_disposable_tests(timeout_seconds) if run_tests else {"skipped": True}
    checks = {
        "canonical_lifespan_present": "async def _app_lifespan" in app_source,
        "startup_guards_present": not any(
            marker in missing_markers
            for marker in (
                "validate_loopback_listener_host",
                "caller_auth_configuration_error",
                "_memory_store_state",
                "_wait_for_required_storage_ready",
            )
        ),
        "finally_cleanup_present": not any(
            marker in missing_markers
            for marker in (
                "_stop_hook_queue_workers",
                "_cancel_lifespan_task",
                "_cleanup_registered_infra_processes",
            )
        ),
        "scenario_tests_present": not missing_tests,
        "disposable_matrix_passed": bool(tests.get("ok")) if run_tests else False,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": all(checks.values()),
        "bounded": True,
        "read_only": True,
        "writes_live_state": False,
        "scope": {
            "startup": ["loopback_guard", "caller_auth_guard", "sqlite_authority_readiness", "required_storage_readiness"],
            "shutdown": ["worker_stop", "background_task_cancel_and_observe", "registered_infrastructure_cleanup"],
            "partial_start": ["worker_start_failure", "background_task_failure", "worker_stop_failure"],
        },
        "checks": checks,
        "missing_tests": missing_tests,
        "missing_lifespan_markers": missing_markers,
        "disposable_tests": tests,
        "test_file": str(TEST_PATH.relative_to(ROOT)),
        "authoritative_mutation": "none",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-tests", action="store_true", help="only inspect source/test matrix")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.timeout_seconds < 1.0 or args.timeout_seconds > 300.0:
        parser.error("--timeout-seconds must be between 1 and 300")
    report = validate(run_tests=not args.skip_tests, timeout_seconds=args.timeout_seconds)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        _write_report(args.output, rendered)
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
