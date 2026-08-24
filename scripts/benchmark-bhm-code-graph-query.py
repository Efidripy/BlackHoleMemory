"""Synthetic WI-03 query/explain latency benchmark."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

import argparse
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path

from blackholememory.code_graph import build_code_graph
from blackholememory.code_graph_query import explain_code_graph
from blackholememory.code_graph_query import query_code_graph
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def _file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fixture(root: Path, files: int) -> None:
    for index in range(files):
        path = root / "src" / f"module_{index:04d}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"from module_{max(0, index - 1):04d} import function_{max(0, index - 1)}\n\n"
            f"def function_{index}():\n"
            f"    return function_{max(0, index - 1)}()\n",
            encoding="utf-8",
        )
    (root / "src" / "routes.py").write_text("from fastapi import APIRouter\nrouter=APIRouter()\n@router.get('/bench')\ndef bench():\n    return 1\n", encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_routes.py").write_text("from src.routes import bench\n\ndef test_bench():\n    assert bench() == 1\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=80)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--p95-budget-ms", type=float, default=2_000.0)
    parser.add_argument("--report")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="bhm-wi03-benchmark-") as temp:
        base = Path(temp)
        root = base / "repo"
        root.mkdir()
        _fixture(root, max(4, args.files))
        database = base / "graph.sqlite3"
        source = RepositorySourceProvenance(owner="WI-03 benchmark", source_url="local://wi03-benchmark", license="MIT fixture", evidence_class="E0")
        indexed = index_repository(root, database, project="benchmark", source=source)
        state = probe_repository_state(root, project="benchmark")
        graph = build_code_graph(database, project="benchmark", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
        operations = [
            ("symbol", "function_20", False),
            ("callers", "function_20", False),
            ("callees", "function_20", False),
            ("imports", "module_0020.py", False),
            ("routes", "/bench", False),
            ("tests", "bench", False),
            ("impact", "module_0020.py", False),
            ("neighborhood", "function_20", True),
        ]
        operation_inputs = {
            operation: (query, explain)
            for operation, query, explain in operations
        }
        durations: list[float] = []
        samples: list[dict] = []
        database_files = [database, database.with_name(database.name + "-wal"), database.with_name(database.name + "-shm")]
        before_mtimes = {str(path): path.stat().st_mtime_ns for path in database_files if path.exists()}
        before_main_digest = _file_digest(database)
        for iteration in range(max(1, args.iterations)):
            for operation, query, explain in operations:
                started = time.perf_counter()
                result = (explain_code_graph if explain else query_code_graph)(database, project="benchmark", root_id=state.root_id, operation=operation, query=query, depth=2, limit=32, max_tokens=4_096, time_budget_ms=2_000.0)
                duration_ms = (time.perf_counter() - started) * 1_000
                durations.append(duration_ms)
                if iteration == 0:
                    samples.append({"operation": operation, "query": query, "explain": explain, "duration_ms": round(duration_ms, 3), "node_count": len(result["nodes"]), "edge_count": len(result["edges"]), "response_digest": result["response_digest"], "truncated": result["bounds"]["truncated"]})
        p95 = statistics.quantiles(durations, n=20, method="inclusive")[18] if len(durations) >= 2 else durations[0]
        deterministic = True
        for sample in samples:
            operation = str(sample["operation"])
            query, explain = operation_inputs[operation]
            repeat = (explain_code_graph if explain else query_code_graph)(
                database,
                project="benchmark",
                root_id=state.root_id,
                operation=operation,
                query=query,
                depth=2,
                limit=32,
                max_tokens=4_096,
                time_budget_ms=2_000.0,
            )
            deterministic = deterministic and repeat["response_digest"] == sample["response_digest"]
        after_mtimes = {str(path): path.stat().st_mtime_ns for path in database_files if path.exists()}
        after_main_digest = _file_digest(database)
        checks = {
            "graph_complete": bool(graph["ok"]),
            "all_operations_return": len(samples) == len(operations) and all(sample["node_count"] >= 0 for sample in samples),
            "p95_budget": p95 <= args.p95_budget_ms,
            "deterministic_response_digest": deterministic,
            "no_writes": before_main_digest == after_main_digest,
        }
        report = {"schema_version": "bhm.code-graph-query-benchmark.v1", "ok": all(checks.values()), "fixture": {"source_files": args.files, "iterations": args.iterations, "graph_nodes": graph["summary"]["node_count"], "graph_edges": graph["summary"]["edge_count"]}, "latency": {"sample_count": len(durations), "p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3)}, "samples": samples, "budget_ms": args.p95_budget_ms, "checks": checks, "sqlite_main_digest_before": before_main_digest, "sqlite_main_digest_after": after_main_digest, "sqlite_sidecars_before": before_mtimes, "sqlite_sidecars_after": after_mtimes, "writes_live_state": False, "writes_qdrant": False, "model_started": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        target = Path(args.report).expanduser().resolve()
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
