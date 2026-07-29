"""Deterministic WI-11 unified MCP/hooks/agent-adapter exit validator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from blackholememory import app as bhm_app
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES
from blackholememory.unified_mcp_contract import build_unified_mcp_contract
from blackholememory.unified_mcp_contract import verify_unified_mcp_contract_digest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "bhm-unified-mcp.py"
BENCHMARK = ROOT / "scripts" / "benchmark-bhm-wi11-unified-mcp.py"


def _hidden_api() -> bool:
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    route = routes.get("/bhm/mcp/unified-contract/preview")
    return route is not None and route.include_in_schema is False


def _fixture() -> dict:
    return {"native_mcp": {"attached": False, "current_session_verified": False, "runtime_lease_live": False, "probe_ok": True, "reason_code": "no_live_native_lease"}}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    args = parser.parse_args()
    fixture = _fixture()
    contract = build_unified_mcp_contract(**fixture)
    schema_hash = contract["catalog"]["schema_hash"]
    aligned = build_unified_mcp_contract(
        **fixture,
        client_snapshots=[
            {"client": "codex", "server_id": "bhm", "schema_hash": schema_hash, "status": "degraded", "rest_bridge": True},
            {"client": "claude", "server_id": "bhm", "schema_hash": schema_hash, "status": "degraded", "rest_bridge": True},
        ],
    )
    mismatch = build_unified_mcp_contract(
        **fixture,
        client_snapshots=[
            {"client": "codex", "server_id": "bhm", "schema_hash": "bad", "status": "degraded", "rest_bridge": True},
            {"client": "claude", "server_id": "bhm", "schema_hash": schema_hash, "status": "degraded", "rest_bridge": True},
        ],
    )
    checks = {
        "schema_digest": contract["schema_version"] == "bhm.mcp.unified-contract.v1" and verify_unified_mcp_contract_digest(contract),
        "one_canonical_namespace": contract["checks"]["one_canonical_namespace"] and contract["namespaces"] == ["bhm"],
        "public_core_12_tools": len(CORE_TOOL_NAMES) == 12 and contract["checks"]["public_core_12_tools"],
        "client_matrix_alignment": aligned["checks"]["client_matrix_aligned"] and not aligned["issues"],
        "schema_mismatch_fail_closed": any(item.get("code") == "schema_hash_mismatch" for item in mismatch["issues"]),
        "hook_idempotency_bounds": contract["checks"]["hooks_idempotent_bounded_observable"] and len(contract["hooks"]) == 6,
        "truthful_degraded_mode": contract["checks"]["truthful_degraded_mode"] and contract["degraded_mode"]["status"] == "MCP unavailable",
        "no_live_writes": all(value is False for value in contract["execution"].values() if isinstance(value, bool)),
        "hidden_api": _hidden_api(),
        "cli_smoke": False,
        "benchmark": False,
    }
    with tempfile.TemporaryDirectory(prefix="bhm-wi11-validator-") as raw:
        temp = Path(raw)
        fixture_path = temp / "fixture.json"
        fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        cli_report = temp / "cli.json"
        cli = subprocess.run([sys.executable, str(CLI), "--fixture", str(fixture_path), "--report", str(cli_report)], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        cli_payload = json.loads(cli_report.read_text(encoding="utf-8")) if cli_report.exists() else {}
        checks["cli_smoke"] = cli.returncode == 0 and cli_payload.get("contract_digest") == contract.get("contract_digest")
        benchmark_report = temp / "benchmark.json"
        benchmark = subprocess.run([sys.executable, str(BENCHMARK), "--iterations", "12", "--p95-budget-ms", "250", "--report", str(benchmark_report)], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        benchmark_payload = json.loads(benchmark_report.read_text(encoding="utf-8")) if benchmark_report.exists() else {}
        checks["benchmark"] = benchmark.returncode == 0 and benchmark_payload.get("ok") is True
        details = {"contract_digest": contract["contract_digest"], "schema_hash": contract["catalog"]["schema_hash"], "benchmark": benchmark_payload.get("latency", {})}
    failed = [name for name, value in checks.items() if not value]
    report = {
        "schema_version": "bhm.wi11.unified-mcp-validation.v1",
        "ok": not failed,
        "check_count": len(checks),
        "passed_count": len(checks) - len(failed),
        "checks": checks,
        "failed": failed,
        "details": details,
        "writes_live_state": False,
        "native_mcp_claimed": False,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
