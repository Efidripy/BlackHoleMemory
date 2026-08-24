"""Deterministic WI-13 capability router exit validator."""

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
from blackholememory.capability_router import build_capability_route_plan
from blackholememory.capability_router import verify_capability_route_digest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "bhm-capability-router.py"
BENCHMARK = ROOT / "scripts" / "benchmark-bhm-capability-router.py"
WI13_PROCESS_TIMEOUT_SECONDS = PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS


def _hidden_api() -> bool:
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    route = routes.get("/bhm/capability-router/preview")
    return route is not None and route.include_in_schema is False


def _run_bounded_child(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run disposable WI-13 children with a finite wait."""

    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=WI13_PROCESS_TIMEOUT_SECONDS,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    args = parser.parse_args()
    local = build_capability_route_plan("retrieval", project="fixture", scope="src", confidence=0.9, evidence_count=2)
    architecture = build_capability_route_plan("architecture", project="fixture", scope="src", confidence=0.9, evidence_count=2)
    blocked = build_capability_route_plan("code-index", project="fixture", scope="src", claim_state={"conflict": True})
    checks = {
        "schema_digest": verify_capability_route_digest(local) and local["schema_version"] == "bhm.capability-router.v1",
        "local_route_measured": local["destination"] == "local" and local["model_route"]["status"] == "routed",
        "codex_final_integrator": architecture["destination"] == "codex" and architecture["governance"]["final_integrator"] == "codex:/root",
        "claim_conflict_blocks": blocked["destination"] == "review" and blocked["checks"]["approval_gate_for_risk"],
        "fallback_is_explicit": local["fallback"]["destination"] in {"codex", "review"} and local["fallback"]["cloud_allowed"] is False,
        "validators_and_review": bool(architecture["validators"]) and "human_review" in architecture["validators"],
        "one_change_stream": all(value for key, value in architecture["checks"].items() if key in {"final_integrator_is_codex_root", "one_change_stream", "no_parallel_authoritative_writes", "cloud_fallback_disabled", "claims_not_started"}),
        "no_live_execution": all(value is False for value in local["execution"].values() if isinstance(value, bool)),
        "hidden_api": _hidden_api(),
        "cli_smoke": False,
        "benchmark": False,
    }
    with tempfile.TemporaryDirectory(prefix="bhm-wi13-validator-") as raw:
        temp = Path(raw)
        fixture_path = temp / "fixture.json"
        fixture_path.write_text(json.dumps({"task_type": "retrieval", "project": "fixture", "scope": "src", "confidence": 0.9, "evidence_count": 2}, ensure_ascii=False), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        cli_report = temp / "cli.json"
        cli = _run_bounded_child([sys.executable, str(CLI), "--fixture", str(fixture_path), "--report", str(cli_report)], cwd=ROOT, env=env)
        cli_payload = json.loads(cli_report.read_text(encoding="utf-8")) if cli_report.exists() else {}
        checks["cli_smoke"] = cli.returncode == 0 and cli_payload.get("route_digest") == local.get("route_digest")
        benchmark_report = temp / "benchmark.json"
        benchmark = _run_bounded_child([sys.executable, str(BENCHMARK), "--iterations", "24", "--p95-budget-ms", "250", "--report", str(benchmark_report)], cwd=ROOT, env=env)
        benchmark_payload = json.loads(benchmark_report.read_text(encoding="utf-8")) if benchmark_report.exists() else {}
        checks["benchmark"] = benchmark.returncode == 0 and benchmark_payload.get("ok") is True
        details = {"local_route_digest": local["route_digest"], "architecture_destination": architecture["destination"], "blocked_destination": blocked["destination"], "benchmark": benchmark_payload.get("latency", {})}
    failed = [name for name, value in checks.items() if not value]
    report = {"schema_version": "bhm.wi13.capability-router-validation.v1", "ok": not failed, "check_count": len(checks), "passed_count": len(checks) - len(failed), "checks": checks, "failed": failed, "details": details, "writes_live_state": False, "model_started": False, "agent_started": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
