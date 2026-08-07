"""Deterministic offline WI-10 factory integration exit validator."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from blackholememory.resource_limits import PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS
from blackholememory import app as bhm_app
from blackholememory.factory_integration import build_factory_integration_preview
from blackholememory.factory_integration import verify_factory_integration_digest
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "scripts" / "bhm-factories.py"
BENCHMARK_PATH = ROOT / "scripts" / "benchmark-bhm-wi10-factories.py"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
WI10_PROCESS_TIMEOUT_SECONDS = PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS
WI10_EXPECTED_CORE_TOOL_COUNT = 35


def _fixture():
    return {"artifacts": [{"id": "failure", "kind": "test", "path": "tests/test_graph.py", "status": "failure", "content": "assertion failed", "severity": "high"}, {"id": "incident", "kind": "incident", "path": "ops/incident.log", "status": "failure", "content": "same signature", "severity": "medium"}], "documents": [{"path": "README.md", "content": "# README\nSee [missing](docs/missing.md)\n"}, {"path": ".docs/adr/0001.md", "content": "# Context\n# Decision\n"}], "changed_paths": ["src/graph.py"], "code_items": [{"path": "src/graph.py", "symbol": "build_graph", "test_paths": ["tests/test_graph.py"], "source_ref": "src/graph.py#build_graph"}], "task_items": [{"task_id": "task-1", "files_touched": ["src/graph.py"], "evidence_refs": ["tests/test_graph.py"], "source_ref": "task:task-1"}]}


def _api_hidden() -> bool:
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    route = routes.get("/bhm/factories/preview")
    return route is not None and route.include_in_schema is False


def _run_bounded_child(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run disposable WI-10 children with a finite wait."""

    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=WI10_PROCESS_TIMEOUT_SECONDS,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    args = parser.parse_args()
    fixture = _fixture()
    preview = build_factory_integration_preview(fixture["artifacts"], fixture["documents"], project="fixture", changed_paths=fixture["changed_paths"], code_items=fixture["code_items"], task_items=fixture["task_items"], risk_class="high", max_items=16, now=NOW)
    checks = {
        "schema_digest": preview["schema_version"] == "bhm.factory-integration.v1" and verify_factory_integration_digest(preview),
        "code_task_test_crosswalk": preview["crosswalk"][0]["test_refs"] == ["tests/test_graph.py"] and preview["crosswalk"][0]["task_refs"] == ["task:task-1"],
        "qa_incident_evidence": preview["qa"]["gates"]["all_verdicts_have_evidence"] is True and preview["qa"]["summary"]["cluster_count"] >= 1,
        "documentation_findings": preview["documentation"]["summary"]["broken_link_count"] == 1 and preview["documentation"]["summary"]["patch_count"] >= 1,
        "human_review_risk_gate": bool(preview["review_queue"]) and all(item["requires_human_review"] for item in preview["review_queue"]),
        "no_raw_or_secret_output": preview["gates"]["raw_log_output"] is False and preview["gates"]["secret_output"] is False,
        "proposal_no_writes": preview["execution"]["writes_performed"] is False and preview["execution"]["tests_started"] is False and preview["execution"]["documents_written"] is False and preview["execution"]["model_started"] is False,
        "public_mcp_unchanged": len(CORE_TOOL_NAMES) == WI10_EXPECTED_CORE_TOOL_COUNT,
        "hidden_api": _api_hidden(),
    }
    with tempfile.TemporaryDirectory(prefix="bhm-wi10-validator-") as raw:
        temp = Path(raw)
        fixture_path = temp / "fixture.json"
        fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        cli_report = temp / "cli.json"
        cli = _run_bounded_child([sys.executable, str(CLI_PATH), "--action", "preview", "--fixture", str(fixture_path), "--project", "fixture", "--risk-class", "high", "--report", str(cli_report)], cwd=ROOT, env=env)
        cli_payload = json.loads(cli_report.read_text(encoding="utf-8")) if cli_report.exists() else {}
        checks["cli_smoke"] = cli.returncode == 0 and cli_payload.get("schema_version") == "bhm.factory-integration.v1" and cli_payload.get("execution", {}).get("writes_performed") is False
        benchmark_report = temp / "benchmark.json"
        benchmark = _run_bounded_child([sys.executable, str(BENCHMARK_PATH), "--items", "16", "--iterations", "8", "--p95-budget-ms", "250", "--report", str(benchmark_report)], cwd=ROOT, env=env)
        benchmark_payload = json.loads(benchmark_report.read_text(encoding="utf-8")) if benchmark_report.exists() else {}
        checks["latency_benchmark"] = benchmark.returncode == 0 and benchmark_payload.get("ok") is True and benchmark_payload.get("checks", {}).get("p95_budget") is True
        details = {"preview_digest": preview["preview_digest"], "summary": {"qa": preview["qa"]["summary"], "documentation": preview["documentation"]["summary"], "crosswalk_count": len(preview["crosswalk"])}, "benchmark": benchmark_payload.get("latency", {})}
    failed = [name for name, value in checks.items() if not value]
    report = {"schema_version": "bhm.wi10.factories-validation.v1", "ok": not failed, "check_count": len(checks), "passed_count": len(checks) - len(failed), "checks": checks, "failed": failed, "details": details, "writes_live_state": False, "model_started": False, "auto_apply": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
