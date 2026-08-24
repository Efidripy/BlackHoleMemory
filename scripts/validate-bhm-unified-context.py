"""Deterministic offline WI-08 unified retrieval/context exit validator."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from blackholememory.resource_limits import PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS
from blackholememory import app as bhm_app
from blackholememory.code_graph import build_code_graph
from blackholememory.convention_memory import build_convention_memory
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state
from blackholememory.unified_context import build_unified_context_from_graph
from blackholememory.unified_context import compile_unified_context


ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "scripts" / "bhm-unified-context.py"
BENCHMARK_PATH = ROOT / "scripts" / "benchmark-bhm-unified-context.py"
WI08_PROCESS_TIMEOUT_SECONDS = PROCESS_EXECUTION_VALIDATOR_TIMEOUT_SECONDS
WI08_EXPECTED_CORE_TOOL_COUNT = 35


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _api_hidden() -> bool:
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    expected = {"/bhm/context/unified/compile"}
    return expected.issubset(routes) and all(getattr(routes[path], "include_in_schema", False) is False for path in expected)


def _fixture(root: Path) -> None:
    (root / "tests").mkdir(parents=True)
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "main.py").write_text("def run_value():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_main.py").write_text("from main import run_value\n\ndef test_run_value():\n    assert run_value() == 1\n", encoding="utf-8")
    (root / "docs" / "adr" / "0001.md").write_text("# ADR\n", encoding="utf-8")


def _source_items() -> dict[str, list[dict[str, object]]]:
    return {
        "memory": [{"id": "memory-1", "title": "Memory", "content": "fact", "source_refs": ["memory:1"]}],
        "code": [{"id": "code-1", "title": "Code", "content": "code metadata", "source_refs": ["src/main.py#L1"]}],
        "conventions": [{"id": "convention-1", "title": "Convention", "content": "proposal", "source_refs": [".docs/adr/0001.md#L1"]}],
        "tasks": [{"id": "task-1", "title": "Task", "content": "task evidence", "source_refs": ["task:1"]}],
        "docs": [{"id": "doc-1", "title": "Docs", "content": "doc evidence", "source_refs": ["docs/readme.md#L1"]}],
        "ops": [{"id": "ops-1", "title": "Ops", "content": "ops evidence", "source_refs": ["ops/slo.json"]}],
    }


def _run_bounded_child(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run disposable WI-08 children with a finite wait."""

    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=WI08_PROCESS_TIMEOUT_SECONDS,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    args = parser.parse_args()
    checks: dict[str, bool] = {}
    details: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="bhm-wi08-validator-") as raw:
        temp = Path(raw)
        root = temp / "repo"
        root.mkdir()
        _fixture(root)
        database = temp / "graph.sqlite3"
        source = RepositorySourceProvenance(owner="WI-08 validator", source_url="local://wi08-fixture", license="synthetic fixture", evidence_class="E0")
        indexed = index_repository(root, database, project="fixture", source=source)
        state = probe_repository_state(root, project="fixture")
        graph = build_code_graph(database, project="fixture", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
        built_conventions = build_convention_memory(database, project="fixture", root_id=state.root_id, graph_snapshot_id=graph["graph_snapshot_id"])
        sources = _source_items()
        first = compile_unified_context(sources, project="fixture", query="run", token_budget=128, max_items_per_source=8)
        repeat = compile_unified_context(sources, project="fixture", query="run", token_budget=128, max_items_per_source=8)
        checks["six_source_channels"] = all(first["sources"]["requested"].get(source_name, 0) == 1 for source_name in ("code", "conventions", "tasks", "docs", "ops", "memory"))
        checks["deterministic_digest"] = first["response_digest"] == repeat["response_digest"]
        checks["bounded_budget_and_truncation"] = first["estimated_tokens"] <= first["token_budget"] and isinstance(first["truncated"], bool) and isinstance(first["omissions"], dict)
        checks["provenance_complete"] = first["provenance"]["complete"] is True and first["provenance"]["evidence_coverage"]["ratio"] == 1.0
        checks["no_writes_or_public_mcp_drift"] = first["execution"]["writes_sqlite_state"] is False and first["execution"]["writes_qdrant"] is False and first["execution"]["public_mcp_changed"] is False and len(CORE_TOOL_NAMES) == WI08_EXPECTED_CORE_TOOL_COUNT

        before = _digest(database)
        graph_result = build_unified_context_from_graph(database, project="fixture", root_id=state.root_id, query="run_value", memory_items=[{"id": "memory-1", "content": "fact", "source_refs": ["memory:1"]}], task_items=[{"id": "task-1", "content": "task", "source_refs": ["task:1"]}], doc_items=[{"id": "doc-1", "content": "doc", "source_refs": ["docs/readme.md#L1"]}], ops_items=[{"id": "ops-1", "content": "ops", "source_refs": ["ops/slo.json"]}], include_code=True, include_conventions=True, include_proposals=True, token_budget=700, limit=8)
        after = _digest(database)
        checks["graph_and_convention_channels"] = graph_result["diagnostics"]["code"]["graph_digest"] == graph["graph_digest"] and graph_result["diagnostics"]["conventions"]["available"] is True and graph_result["sources"]["included"].get("code", 0) >= 1 and graph_result["sources"]["included"].get("conventions", 0) >= 1
        checks["graph_preview_read_only"] = before == after and graph_result["execution"]["writes_sqlite_state"] is False and graph_result["execution"]["raw_source_returned"] is False
        checks["internal_api_hidden"] = _api_hidden()
        checks["convention_proposals_are_explicit"] = built_conventions["summary"]["proposal_count"] == built_conventions["summary"]["card_count"] and all(item["context_origin"] == "PROPOSAL" for item in graph_result["citations"] if (item.get("provenance") or {}).get("source_kind") == "conventions")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        items_path = temp / "items.json"
        items_path.write_text(json.dumps(_source_items()), encoding="utf-8")
        cli_report = temp / "cli.json"
        cli = _run_bounded_child([sys.executable, str(CLI_PATH), "--action", "compile", "--items-file", str(items_path), "--project", "fixture", "--query", "run", "--token-budget", "600", "--report", str(cli_report)], cwd=ROOT, env=env)
        cli_payload = json.loads(cli_report.read_text(encoding="utf-8")) if cli_report.exists() else {}
        checks["cli_smoke"] = cli.returncode == 0 and cli_payload.get("schema_version") == "bhm.unified-context.v1" and cli_payload.get("execution", {}).get("writes_sqlite_state") is False
        benchmark_report = temp / "benchmark.json"
        benchmark = _run_bounded_child([sys.executable, str(BENCHMARK_PATH), "--items-per-source", "20", "--iterations", "8", "--p95-budget-ms", "250", "--report", str(benchmark_report)], cwd=ROOT, env=env)
        benchmark_payload = json.loads(benchmark_report.read_text(encoding="utf-8")) if benchmark_report.exists() else {}
        checks["latency_benchmark_green"] = benchmark.returncode == 0 and benchmark_payload.get("ok") is True and benchmark_payload.get("checks", {}).get("p95_budget") is True
        details = {"graph_snapshot_id": graph["graph_snapshot_id"], "graph_digest": graph["graph_digest"], "convention_snapshot_id": built_conventions["convention_snapshot_id"], "convention_digest": built_conventions["convention_digest"], "response_digest": graph_result["response_digest"], "sources": graph_result["sources"], "benchmark": benchmark_payload.get("latency", {})}
    failed = [name for name, value in checks.items() if not value]
    report = {"schema_version": "bhm.wi08.unified-context-validation.v1", "ok": not failed, "check_count": len(checks), "passed_count": len(checks) - len(failed), "checks": checks, "failed": failed, "details": details, "writes_live_state": False, "writes_qdrant": False, "model_started": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        output = Path(args.report).expanduser().resolve()
        replace_bytes_safely(output, (rendered + "\n").encode("utf-8"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
