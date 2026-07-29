"""Deterministic offline WI-06 temporal memory graph exit validator."""

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
from blackholememory.memory_graph import MemoryGraphError
from blackholememory.memory_graph import build_memory_graph
from blackholememory.memory_graph import query_memory_graph
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "scripts" / "bhm-memory-graph.py"
BENCHMARK_PATH = ROOT / "scripts" / "benchmark-bhm-wi06-memory-graph.py"


def _fixture() -> dict[str, list[dict]]:
    records = [
        {"source_id": "mem-old", "project": "fixture", "memory_type": "fact", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "metadata": {"raw_title": "old", "source_refs": ["session:s1"]}},
        {"source_id": "mem-new", "project": "fixture", "memory_type": "fact", "created_at": "2026-02-01T00:00:00Z", "updated_at": "2026-02-01T00:00:00Z", "metadata": {"raw_title": "new", "supersedes": "mem-old", "source_refs": ["adr:1"]}},
        {"source_id": "mem-invalid", "project": "fixture", "valid_from": "2026-03-01T00:00:00Z", "valid_until": "2026-02-01T00:00:00Z", "recorded_at": "2026-03-01T00:00:00Z"},
        {"source_id": "mem-cross", "project": "other", "memory_type": "fact", "created_at": "2026-01-01T00:00:00Z"},
    ]
    links = [
        {"source_id": "mem-new", "target_id": "mem-old", "relation": "supersedes", "project": "fixture", "valid_from": "2026-02-01T00:00:00Z"},
        {"source_id": "mem-new", "target_id": "missing", "relation": "related_to", "project": "fixture", "valid_from": "2026-02-01T00:00:00Z"},
    ]
    return {"records": records, "links": links, "observations": [], "session_records": [{"id": "session-1", "project": "fixture", "created_at": "2026-01-01T00:00:00Z"}], "tasks": [], "adrs": [], "documents": []}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _api_hidden() -> bool:
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    return all(path in routes and routes[path].include_in_schema is False for path in ("/bhm/memory-graph/query", "/bhm/memory-graph/explain"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    args = parser.parse_args()
    fixture = _fixture()
    with tempfile.TemporaryDirectory(prefix="bhm-wi06-validator-") as raw:
        temp = Path(raw)
        database = temp / "memory.sqlite3"
        built = build_memory_graph(database, project="fixture", records=fixture["records"], links=fixture["links"], session_records=fixture["session_records"])
        current = query_memory_graph(database, project="fixture", operation="as_of", limit=32)
        before = _digest(database)
        before_replay = query_memory_graph(database, project="fixture", operation="as_of", as_of="2026-01-15T00:00:00Z", limit=32)
        after_replay = query_memory_graph(database, project="fixture", operation="as_of", as_of="2026-02-15T00:00:00Z", limit=32)
        checks: dict[str, bool] = {}
        checks["build_schema_and_digest"] = built["ok"] is True and built["schema_version"] == "bhm.memory-graph.v1" and len(built["graph_digest"]) == 64
        checks["temporal_quarantine"] = built["summary"]["invalid_temporal_count"] == 1 and built["summary"]["unresolved_edge_count"] == 1
        checks["as_of_replay"] = {item["entity_id"] for item in before_replay["nodes"]} == {"mem-old", "session-1"} and {item["entity_id"] for item in after_replay["nodes"]} == {"mem-old", "mem-new", "session-1"}
        checks["supersession_edge"] = any(item["relation"] == "supersedes" for item in query_memory_graph(database, project="fixture", operation="supersession")["edges"])
        checks["provenance_complete"] = current["provenance"]["complete"] is True and all(item.get("provenance") for item in current["nodes"] + current["edges"])
        checks["project_isolation"] = query_memory_graph(database, project="fixture", operation="search", query="mem-cross")["nodes"] == []
        checks["read_only_query"] = before == _digest(database) and current["execution"]["writes_sqlite"] is False
        try:
            build_memory_graph(database, project="fixture", records=fixture["records"][:1], fail_after_stage="before_publish")
        except MemoryGraphError:
            pass
        rollback = query_memory_graph(database, project="fixture")
        checks["lkg_rollback"] = rollback["snapshot_id"] == current["snapshot_id"]
        checks["bounded_budget"] = current["budget"]["estimated_tokens"] <= current["budget"]["max_tokens"] and current["budget"]["within_time_budget"] is True
        checks["hidden_api"] = _api_hidden()
        checks["public_mcp_unchanged"] = len(CORE_TOOL_NAMES) == 12
        fixture_path = temp / "fixture.json"
        fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        cli_report = temp / "cli.json"
        cli = subprocess.run([sys.executable, str(CLI_PATH), "--action", "build", "--database", str(temp / "cli.sqlite3"), "--fixture", str(fixture_path), "--project", "fixture", "--report", str(cli_report)], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        cli_payload = json.loads(cli_report.read_text(encoding="utf-8")) if cli_report.exists() else {}
        checks["cli_smoke"] = cli.returncode == 0 and cli_payload.get("schema_version") == "bhm.memory-graph.v1" and cli_payload.get("summary", {}).get("node_count") == 3
        benchmark_report = temp / "benchmark.json"
        benchmark = subprocess.run([sys.executable, str(BENCHMARK_PATH), "--items", "24", "--iterations", "8", "--p95-budget-ms", "250", "--report", str(benchmark_report)], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        benchmark_payload = json.loads(benchmark_report.read_text(encoding="utf-8")) if benchmark_report.exists() else {}
        checks["latency_benchmark"] = benchmark.returncode == 0 and benchmark_payload.get("ok") is True and benchmark_payload.get("checks", {}).get("query_p95_budget") is True
        details = {"snapshot_id": built["snapshot_id"], "graph_digest": built["graph_digest"], "summary": built["summary"], "replay_before": before_replay["response_digest"], "replay_after": after_replay["response_digest"], "benchmark": benchmark_payload.get("latency", {})}
    failed = [name for name, value in checks.items() if not value]
    report = {"schema_version": "bhm.wi06.memory-graph-validation.v1", "ok": not failed, "check_count": len(checks), "passed_count": len(checks) - len(failed), "checks": checks, "failed": failed, "details": details, "writes_live_state": False, "writes_qdrant": False, "model_started": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
