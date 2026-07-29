"""Deterministic offline WI-07 task graph/governance exit validator."""

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
from blackholememory.task_graph import TaskGraphError
from blackholememory.task_graph import build_task_graph
from blackholememory.task_graph import query_task_graph
from blackholememory.task_graph import simulate_conflict_recovery_fixture


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "scripts" / "bhm-task-graph.py"
BENCHMARK_PATH = ROOT / "scripts" / "benchmark-bhm-wi07-task-graph.py"


def _fixture() -> dict[str, list[dict]]:
    tasks = [{"task_id": "task-base", "project": "fixture", "status": "closed", "created_at": "2026-01-01T00:00:00Z"}, {"task_id": "task-main", "project": "fixture", "status": "open", "dependencies": ["task-base"], "created_at": "2026-01-02T00:00:00Z"}, {"task_id": "cycle-a", "project": "fixture", "status": "open", "dependencies": ["cycle-b"], "created_at": "2026-01-03T00:00:00Z"}, {"task_id": "cycle-b", "project": "fixture", "status": "open", "dependencies": ["cycle-a"], "created_at": "2026-01-03T00:00:00Z"}, {"task_id": "cross", "project": "other", "status": "open", "created_at": "2026-01-01T00:00:00Z"}]
    claims = [{"claim_id": "claim-a", "task_id": "task-main", "agent_id": "agent-a", "project": "fixture", "expires_at": "2026-12-01T00:00:00Z", "created_at": "2026-01-02T00:00:00Z"}, {"claim_id": "claim-b", "task_id": "task-main", "agent_id": "agent-b", "project": "fixture", "expires_at": "2026-12-01T00:00:00Z", "created_at": "2026-01-02T00:00:00Z"}, {"claim_id": "claim-expired", "task_id": "task-base", "agent_id": "agent-c", "project": "fixture", "expires_at": "2026-01-01T00:00:00Z", "created_at": "2025-12-01T00:00:00Z"}]
    evidence = [{"evidence_id": "evidence-base", "task_id": "task-base", "project": "fixture", "kind": "test", "status": "accepted", "digest": "a" * 64, "created_at": "2026-01-03T00:00:00Z"}]
    events = [{"event_id": "event-conflict", "task_id": "task-main", "project": "fixture", "kind": "claim", "outcome": "conflict", "created_at": "2026-01-02T00:00:00Z"}]
    return {"tasks": tasks, "claims": claims, "evidence": evidence, "events": events}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _api_hidden() -> bool:
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    return all(path in routes and routes[path].include_in_schema is False for path in ("/bhm/task-graph/query", "/bhm/task-graph/explain"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    args = parser.parse_args()
    fixture = _fixture()
    with tempfile.TemporaryDirectory(prefix="bhm-wi07-validator-") as raw:
        temp = Path(raw)
        database = temp / "tasks.sqlite3"
        built = build_task_graph(database, project="fixture", tasks=fixture["tasks"], claims=fixture["claims"], evidence=fixture["evidence"], events=fixture["events"], as_of="2026-02-01T00:00:00Z")
        current = query_task_graph(database, project="fixture", operation="status")
        before = _digest(database)
        checks: dict[str, bool] = {}
        checks["build_schema_and_governance"] = built["ok"] is True and built["schema_version"] == "bhm.task-graph.v1" and built["summary"]["conflict_count"] == 1 and built["summary"]["lease_expired_count"] == 1
        checks["dependency_cycle_quarantine"] = any(item["reason"] == "dependency_cycle" for item in built["quarantine"])
        checks["ready_blocks_conflict"] = all(item["entity_id"] != "task-main" for item in query_task_graph(database, project="fixture", operation="ready")["nodes"])
        checks["dependency_query"] = bool(query_task_graph(database, project="fixture", operation="dependencies", query="task-main")["edges"])
        checks["conflict_query"] = bool(query_task_graph(database, project="fixture", operation="conflicts")["edges"])
        checks["timeline_query"] = any(item["entity_id"] == "event-conflict" for item in query_task_graph(database, project="fixture", operation="timeline")["nodes"])
        checks["evidence_backed_close"] = built["summary"]["evidence_backed_close_count"] == 1
        checks["provenance_complete"] = current["provenance"]["complete"] is True and all(item.get("provenance") for item in current["nodes"] + current["edges"])
        checks["project_isolation"] = query_task_graph(database, project="fixture", operation="status")["nodes"] and all(item["entity_id"] != "cross" for item in current["nodes"])
        checks["read_only_and_bounds"] = before == _digest(database) and current["execution"]["writes_sqlite"] is False and current["budget"]["within_time_budget"] is True
        try:
            build_task_graph(database, project="fixture", tasks=fixture["tasks"][:1], fail_after_stage="before_publish")
        except TaskGraphError:
            pass
        checks["lkg_rollback"] = query_task_graph(database, project="fixture")["snapshot_id"] == current["snapshot_id"]
        checks["fixture_deterministic"] = simulate_conflict_recovery_fixture() == simulate_conflict_recovery_fixture() and simulate_conflict_recovery_fixture()["final"]["evidence_backed"] is True
        checks["hidden_api_and_public_mcp"] = _api_hidden() and len(CORE_TOOL_NAMES) == 12
        fixture_path = temp / "fixture.json"
        fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        cli_report = temp / "cli.json"
        cli = subprocess.run([sys.executable, str(CLI_PATH), "--action", "build", "--database", str(temp / "cli.sqlite3"), "--fixture", str(fixture_path), "--project", "fixture", "--report", str(cli_report)], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        cli_payload = json.loads(cli_report.read_text(encoding="utf-8")) if cli_report.exists() else {}
        checks["cli_smoke"] = cli.returncode == 0 and cli_payload.get("schema_version") == "bhm.task-graph.v1" and cli_payload.get("summary", {}).get("conflict_count") == 1
        benchmark_report = temp / "benchmark.json"
        benchmark = subprocess.run([sys.executable, str(BENCHMARK_PATH), "--items", "24", "--iterations", "8", "--p95-budget-ms", "250", "--report", str(benchmark_report)], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        benchmark_payload = json.loads(benchmark_report.read_text(encoding="utf-8")) if benchmark_report.exists() else {}
        checks["latency_benchmark"] = benchmark.returncode == 0 and benchmark_payload.get("ok") is True and benchmark_payload.get("checks", {}).get("query_p95_budget") is True
        details = {"snapshot_id": built["snapshot_id"], "graph_digest": built["graph_digest"], "summary": built["summary"], "benchmark": benchmark_payload.get("latency", {})}
    failed = [name for name, value in checks.items() if not value]
    report = {"schema_version": "bhm.wi07.task-graph-validation.v1", "ok": not failed, "check_count": len(checks), "passed_count": len(checks) - len(failed), "checks": checks, "failed": failed, "details": details, "writes_live_state": False, "agents_started": False, "writes_qdrant": False, "model_started": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
