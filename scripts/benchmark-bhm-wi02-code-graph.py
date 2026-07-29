"""Synthetic WI-02 canonical code graph benchmark."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from blackholememory.code_graph import build_code_graph
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def _fixture(root: Path, files: int, lines_per_file: int) -> None:
    for index in range(files):
        path = root / "src" / f"module_{index:04d}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        function_count = max(1, min(3, lines_per_file // 3))
        for line in range(function_count):
            lines.extend([f"def function_{index}_{line}():", "    return 1"])
        lines.extend(["# benchmark filler"] * max(0, lines_per_file - len(lines)))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "src" / "routes.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n\n@router.get('/bench')\ndef bench():\n    return 1\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_bench.py").write_text(
        "from src.routes import bench\n\ndef test_bench():\n    assert bench() == 1\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=160)
    parser.add_argument("--lines-per-file", type=int, default=40)
    parser.add_argument("--cold-ms-per-kloc-budget", type=float, default=2_500.0)
    parser.add_argument("--repeat-ms-budget", type=float, default=2_000.0)
    parser.add_argument("--report")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="bhm-wi02-benchmark-") as temp:
        temp_root = Path(temp)
        root = temp_root / "repo"
        root.mkdir()
        _fixture(root, max(1, args.files), max(8, args.lines_per_file))
        database = temp_root / "benchmark.sqlite3"
        source = RepositorySourceProvenance(owner="WI-02 benchmark", source_url="local://wi02-benchmark", license="MIT fixture", evidence_class="E0")
        started = time.perf_counter()
        indexed = index_repository(root, database, project="benchmark", source=source)
        state = probe_repository_state(root, project="benchmark")
        cold = build_code_graph(database, project="benchmark", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
        cold_ms = round((time.perf_counter() - started) * 1_000, 3)
        repeat_started = time.perf_counter()
        repeat = build_code_graph(database, project="benchmark", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
        repeat_ms = round((time.perf_counter() - repeat_started) * 1_000, 3)
        total_lines = max(1, int(cold["summary"].get("file_count", 0)) * max(1, args.lines_per_file))
        kloc = total_lines / 1_000
        checks = {
            "cold_complete": bool(cold["ok"]),
            "repeat_deduplicated": repeat["graph_snapshot_id"] == cold["graph_snapshot_id"] and repeat["graph_digest"] == cold["graph_digest"],
            "cold_budget": cold_ms / max(kloc, 0.001) <= args.cold_ms_per_kloc_budget,
            "repeat_budget": repeat_ms <= args.repeat_ms_budget,
            "parser_error_budget": float(cold["summary"].get("parser_error_rate", 1.0)) <= 0.05,
            "no_live_writes": cold["execution"]["writes_qdrant"] is False and cold["execution"]["model_started"] is False,
        }
        report = {
            "schema_version": "bhm.code-graph-benchmark.v1",
            "ok": all(checks.values()),
            "fixture": {"source_files": args.files, "lines_per_file": args.lines_per_file, "indexed_lines_estimate": total_lines, "kloc": round(kloc, 3)},
            "cold": {"duration_ms": cold_ms, "ms_per_kloc": round(cold_ms / max(kloc, 0.001), 3), "graph_snapshot_id": cold["graph_snapshot_id"], "graph_digest": cold["graph_digest"], "node_count": cold["summary"]["node_count"], "edge_count": cold["summary"]["edge_count"], "parser_error_rate": cold["summary"]["parser_error_rate"]},
            "repeat": {"duration_ms": repeat_ms, "deduplicated": repeat["graph_snapshot_id"] == cold["graph_snapshot_id"], "graph_digest": repeat["graph_digest"]},
            "budgets": {"cold_ms_per_kloc": args.cold_ms_per_kloc_budget, "repeat_ms": args.repeat_ms_budget},
            "checks": checks,
            "writes_live_state": False,
            "writes_qdrant": False,
            "model_started": False,
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        output = Path(args.report).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
