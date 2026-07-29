"""Deterministic offline WI-09 local LLM code-fabric exit validator."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from blackholememory import app as bhm_app
from blackholememory.llm_code_fabric import LLMCodeFabricError
from blackholememory.llm_code_fabric import build_code_fabric_plan
from blackholememory.llm_code_fabric import verify_code_fabric_plan
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "scripts" / "bhm-llm-code-fabric.py"
BENCHMARK_PATH = ROOT / "scripts" / "benchmark-bhm-wi09-llm-code-fabric.py"


def _api_hidden() -> bool:
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    route = routes.get("/bhm/llm/code-fabric/plan")
    return route is not None and route.include_in_schema is False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    args = parser.parse_args()
    checks: dict[str, bool] = {}
    routed = build_code_fabric_plan("code_summary", {"query": "graph", "files": ["src/a.py"]}, project="fixture", context_digest="a" * 64, measurements=[{"context_tokens": 8192, "ok": True, "latency_ms": 20}], confidence=0.9, evidence_count=2)
    checks["schema_digest"] = routed["schema_version"] == "bhm.llm.code-fabric.v1" and verify_code_fabric_plan(routed)
    checks["capability_route"] = routed["route"]["status"] == "routed" and routed["route"]["local_only"] is True
    missing_profile = build_code_fabric_plan("query_expansion", {"query": "x"}, project="fixture", context_tokens=16_384, measurements=[{"context_tokens": 8192, "ok": True}])
    checks["unmeasured_profile_rejected"] = missing_profile["route"]["status"] == "rejected" and "context_profile_not_measured" in missing_profile["route"]["reason_codes"]
    restricted = build_code_fabric_plan("test_plan", {"changed_paths": ["src/a.py"]}, project="fixture", sensitivity="restricted", mutation_requested=True, risk_flags=["security"], confidence=0.9, evidence_count=2)
    checks["restricted_mutation_escalates"] = restricted["policy"]["destination"] in {"codex", "operator"} and restricted["policy"]["approval_required"] is True and restricted["policy"]["mutation_allowed"] is False
    try:
        build_code_fabric_plan("code_summary", {"api_token": "secret"})
    except LLMCodeFabricError:
        checks["sensitive_payload_denied"] = True
    else:
        checks["sensitive_payload_denied"] = False
    checks["proposal_no_model_or_writes"] = not any(bool(routed["execution"].get(key)) for key in ("model_started", "writes_sqlite", "writes_mem0", "writes_qdrant", "writes_langgraph", "auto_apply"))
    checks["public_mcp_unchanged"] = len(CORE_TOOL_NAMES) == 12
    checks["hidden_api"] = _api_hidden()
    with tempfile.TemporaryDirectory(prefix="bhm-wi09-validator-") as raw:
        temp = Path(raw)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        cli_report = temp / "cli.json"
        cli = subprocess.run([sys.executable, str(CLI_PATH), "--action", "preview", "--task-type", "code_summary", "--project", "fixture", "--report", str(cli_report)], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        cli_payload = json.loads(cli_report.read_text(encoding="utf-8")) if cli_report.exists() else {}
        checks["cli_smoke"] = cli.returncode == 0 and cli_payload.get("schema_version") == "bhm.llm.code-fabric.v1" and cli_payload.get("execution", {}).get("model_started") is False
        benchmark_report = temp / "benchmark.json"
        benchmark = subprocess.run([sys.executable, str(BENCHMARK_PATH), "--iterations", "20", "--p95-budget-ms", "100", "--report", str(benchmark_report)], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        benchmark_payload = json.loads(benchmark_report.read_text(encoding="utf-8")) if benchmark_report.exists() else {}
        checks["latency_benchmark"] = benchmark.returncode == 0 and benchmark_payload.get("ok") is True and benchmark_payload.get("checks", {}).get("p95_budget") is True
        details = {"plan_digest": routed["plan_digest"], "route": routed["route"], "policy": routed["policy"], "benchmark": benchmark_payload.get("latency", {})}
    failed = [name for name, value in checks.items() if not value]
    report = {"schema_version": "bhm.wi09.llm-code-fabric-validation.v1", "ok": not failed, "check_count": len(checks), "passed_count": len(checks) - len(failed), "checks": checks, "failed": failed, "details": details, "writes_live_state": False, "model_started": False, "auto_apply": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
