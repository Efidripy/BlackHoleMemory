"""Deterministic offline WI-03 bounded graph query/explain exit validator."""

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
from blackholememory.code_graph import SQLiteCodeGraphStore
from blackholememory.code_graph import build_code_graph
from blackholememory.code_graph_query import ALLOWED_OPERATIONS
from blackholememory.code_graph_query import CODE_GRAPH_EXPLAIN_SCHEMA_VERSION
from blackholememory.code_graph_query import CODE_GRAPH_QUERY_SCHEMA_VERSION
from blackholememory.code_graph_query import CodeGraphQueryError
from blackholememory.code_graph_query import explain_code_graph
from blackholememory.code_graph_query import query_code_graph
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "cbm-integration.json"
REGISTRY_PATH = REPO_ROOT / "config" / "source-registry.json"
CLI_PATH = REPO_ROOT / "scripts" / "bhm-code-graph-query.py"
BENCHMARK_PATH = REPO_ROOT / "scripts" / "benchmark-bhm-wi03-code-graph-query.py"


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _fixture(root: Path) -> None:
    (root / "tests").mkdir(parents=True)
    (root / "main.py").write_text(
        "from service import Service\n"
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n"
        "@router.get('/items')\n"
        "def get_items():\n"
        "    return Service().run()\n",
        encoding="utf-8",
    )
    (root / "service.py").write_text(
        "class Service:\n"
        "    def run(self):\n"
        "        return helper()\n\n"
        "def helper():\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_main.py").write_text(
        "from main import get_items\n\n"
        "def test_get_items():\n"
        "    assert get_items() == 'ok'\n",
        encoding="utf-8",
    )


def _source() -> RepositorySourceProvenance:
    return RepositorySourceProvenance(
        owner="WI-03 validator",
        source_url="local://wi03-fixture",
        license="synthetic fixture",
        evidence_class="E0",
        source_registry_id="WI03-FIXTURE",
    )


def _flags_off() -> bool:
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    flags = payload.get("feature_flags") or {}
    forbidden = {"source_import_enabled", "migration_enabled", "obsidian_bridge_enabled", "autonomous_apply_enabled", "training_enabled", "lora_enabled"}
    return not any(bool(flags.get(name)) for name in forbidden)


def _clean_room_registry() -> bool:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return all(
        item.get("code_copy_allowed") is False
        or (
            item.get("code_copy_allowed") is True
            and item.get("transfer_mode") == "direct-transfer-scoped"
            and item.get("permission_status") == "written-permission"
            and bool(item.get("covered_files"))
        )
        for item in payload.get("sources", [])
    )


def _api_surface() -> bool:
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    expected = {"/bhm/code-graph/query", "/bhm/code-graph/explain"}
    return expected.issubset(routes) and all(getattr(routes[path], "include_in_schema", False) is False for path in expected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    args = parser.parse_args()
    checks: dict[str, bool] = {}
    details: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="bhm-wi03-validator-") as raw:
        temp = Path(raw)
        root = temp / "repo"
        root.mkdir()
        _fixture(root)
        database = temp / "canonical.sqlite3"
        source = _source()
        first_index = index_repository(root, database, project="fixture", source=source)
        state = probe_repository_state(root, project="fixture")
        first_graph = build_code_graph(
            database,
            project="fixture",
            root_id=state.root_id,
            repository_snapshot_id=first_index["snapshot_id"],
        )
        store = SQLiteCodeGraphStore(database)
        material = store.snapshot(first_graph["graph_snapshot_id"], include_material=True)
        provenance_missing = [
            {"node_kind": node.get("node_kind"), "path": node.get("path"), "name": node.get("name"), "provenance": node.get("provenance")}
            for node in material["nodes"]
            if node.get("node_kind") not in {"repository", "repository_snapshot", "external_module", "unresolved_symbol", "service_image", "service_route", "service_endpoint", "service_component", "event_channel", "data_field", "package"}
            and not node.get("provenance", {}).get("source_ref")
            and not (node.get("node_kind") == "route" and (node.get("attributes") or {}).get("path"))
        ]
        checks["live_flags_remain_off"] = _flags_off()
        checks["source_registry_clean_room"] = _clean_room_registry()
        checks["canonical_mcp_core_unchanged"] = len(CORE_TOOL_NAMES) == 31 and "bhm_change_impact_preview" in CORE_TOOL_NAMES
        checks["internal_api_routes_hidden"] = _api_surface()
        checks["graph_schema_ready"] = store.inspect_schema().get("ready") is True
        checks["no_raw_source_in_snapshot"] = all("content" not in node and "raw_source" not in node for node in material["nodes"])
        checks["snapshot_provenance_present"] = bool(material.get("graph_digest")) and all(
            node.get("provenance", {}).get("extractor_version")
            and (
                node.get("provenance", {}).get("source_ref")
                or (node.get("node_kind") == "route" and (node.get("attributes") or {}).get("path"))
                or node.get("node_kind") in {"repository", "repository_snapshot", "external_module", "unresolved_symbol", "service_image", "service_route", "service_endpoint", "service_component", "event_channel", "data_field", "package"}
            )
            for node in material["nodes"]
        )

        queries = {
            "symbol": "get_items",
            "resolve": "Service",
            "callers": "get_items",
            "callees": "get_items",
            "imports": "service.py",
            "importers": "service.py",
            "routes": "/items",
            "tests": "get_items",
            "impact": "service.py",
            "neighborhood": "get_items",
        }
        responses: dict[str, dict] = {}
        for operation in sorted(ALLOWED_OPERATIONS):
            responses[operation] = query_code_graph(
                database,
                project="fixture",
                root_id=state.root_id,
                operation=operation,
                query=queries[operation],
                depth=2,
                limit=32,
                max_tokens=4_096,
                time_budget_ms=2_000,
            )
        checks["allowlisted_operation_matrix"] = len(responses) == len(ALLOWED_OPERATIONS) and all(
            response["schema_version"] == CODE_GRAPH_QUERY_SCHEMA_VERSION
            and response["snapshot_id"] == first_graph["graph_snapshot_id"]
            and response["graph_hash"] == first_graph["graph_digest"]
            and response["execution"]["writes_sqlite_state"] is False
            and response["execution"]["raw_source_returned"] is False
            for response in responses.values()
        )
        first_digest = responses["symbol"]["response_digest"]
        repeat = query_code_graph(database, project="fixture", root_id=state.root_id, operation="symbol", query="get_items", depth=2, limit=32, max_tokens=4_096, time_budget_ms=2_000)
        checks["deterministic_response_digest"] = first_digest == repeat["response_digest"]
        explanation = explain_code_graph(database, project="fixture", root_id=state.root_id, operation="neighborhood", query="get_items", depth=2, limit=32, max_tokens=4_096, time_budget_ms=2_000)
        checks["explain_schema_and_provenance"] = explanation["schema_version"] == CODE_GRAPH_EXPLAIN_SCHEMA_VERSION and bool(explanation["explanations"]) and all("reason" in item and "source_refs" in item for item in explanation["explanations"])

        before_digest = _digest(database)
        before_current = store.current_snapshot("fixture", state.root_id)
        invalid = 0
        for operation, query, kwargs in (("sql", "get_items", {}), ("callers", "../secret", {}), ("callers", "get_items", {"depth": 99}), ("callers", "get_items", {"limit": 129}), ("callers", "get_items", {"max_tokens": 17_000}), ("callers", "get_items", {"time_budget_ms": 5_001})):
            try:
                query_code_graph(database, project="fixture", root_id=state.root_id, operation=operation, query=query, **kwargs)
            except CodeGraphQueryError:
                invalid += 1
        after_digest = _digest(database)
        after_current = store.current_snapshot("fixture", state.root_id)
        checks["invalid_operations_and_bounds_rejected"] = invalid == 6
        checks["read_only_sqlite_boundary"] = before_digest == after_digest and before_current["graph_snapshot_id"] == after_current["graph_snapshot_id"]

        (root / "service.py").write_text((root / "service.py").read_text(encoding="utf-8") + "\n\ndef added():\n    return 2\n", encoding="utf-8")
        second_index = index_repository(root, database, project="fixture", source=source)
        second_graph = build_code_graph(database, project="fixture", root_id=state.root_id, repository_snapshot_id=second_index["snapshot_id"])
        stale = query_code_graph(database, project="fixture", root_id=state.root_id, operation="symbol", query="helper", snapshot_id=first_graph["graph_snapshot_id"], time_budget_ms=2_000)
        checks["stale_snapshot_is_explicit"] = stale["stale"] is True and stale["snapshot_id"] == first_graph["graph_snapshot_id"] and stale["graph_hash"] == first_graph["graph_digest"]

        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        cli_report = temp / "cli-query.json"
        cli = subprocess.run([sys.executable, str(CLI_PATH), "--action", "explain", "--operation", "symbol", "--query", "get_items", "--root", str(root), "--database", str(database), "--project", "fixture", "--root-id", state.root_id, "--time-budget-ms", "2000", "--report", str(cli_report)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        cli_payload = json.loads(cli_report.read_text(encoding="utf-8")) if cli_report.exists() else {}
        checks["cli_read_only_smoke"] = cli.returncode == 0 and cli_payload.get("schema_version") == CODE_GRAPH_EXPLAIN_SCHEMA_VERSION and cli_payload.get("execution", {}).get("writes_sqlite_state") is False

        benchmark_report = temp / "benchmark.json"
        benchmark = subprocess.run([sys.executable, str(BENCHMARK_PATH), "--files", "24", "--iterations", "3", "--p95-budget-ms", "2000", "--report", str(benchmark_report)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        benchmark_payload = json.loads(benchmark_report.read_text(encoding="utf-8")) if benchmark_report.exists() else {}
        checks["latency_benchmark_green"] = benchmark.returncode == 0 and benchmark_payload.get("ok") is True and benchmark_payload.get("checks", {}).get("no_writes") is True
        details = {
            "graph_snapshot_id": first_graph["graph_snapshot_id"],
            "graph_digest": first_graph["graph_digest"],
            "node_count": first_graph["summary"]["node_count"],
            "edge_count": first_graph["summary"]["edge_count"],
            "provenance_missing": provenance_missing,
            "operation_counts": {operation: {"nodes": len(response["nodes"]), "edges": len(response["edges"])} for operation, response in responses.items()},
            "stale_snapshot_id": stale["snapshot_id"],
            "current_snapshot_id": second_graph["graph_snapshot_id"],
            "benchmark": {"p50_ms": benchmark_payload.get("latency", {}).get("p50_ms"), "p95_ms": benchmark_payload.get("latency", {}).get("p95_ms"), "max_ms": benchmark_payload.get("latency", {}).get("max_ms")},
        }
    failed = [name for name, value in checks.items() if not value]
    report = {"schema_version": "bhm.wi03.code-graph-query-validation.v1", "ok": not failed, "check_count": len(checks), "passed_count": len(checks) - len(failed), "checks": checks, "failed": failed, "details": details, "writes_live_state": False, "writes_qdrant": False, "model_started": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        output = Path(args.report).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
