"""Synthetic WI-04 convention extraction and architecture-card benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path

from blackholememory.code_graph import build_code_graph
from blackholememory.convention_memory import build_convention_memory
from blackholememory.convention_memory import preview_convention_memory
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _fixture(root: Path, files: int) -> None:
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "adr").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    for index in range(max(4, files)):
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / f"module_{index:04d}.py").write_text(
            f"from module_{max(0, index - 1):04d} import function_{max(0, index - 1)}\n\n"
            f"def function_{index}():\n    return function_{max(0, index - 1)}()\n",
            encoding="utf-8",
        )
    (root / "tests" / "test_routes.py").write_text("from src.module_0000 import function_0\n\ndef test_function_0():\n    assert function_0() is not None\n", encoding="utf-8")
    (root / "docs" / "adr" / "0001-architecture.md").write_text("# ADR-0001\n", encoding="utf-8")
    (root / "config" / "settings.json").write_text("{}\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=80)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--p95-budget-ms", type=float, default=2_000.0)
    parser.add_argument("--report")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="bhm-wi04-benchmark-") as raw:
        temp = Path(raw)
        root = temp / "repo"
        root.mkdir()
        _fixture(root, args.files)
        database = temp / "graph.sqlite3"
        source = RepositorySourceProvenance(owner="WI-04 benchmark", source_url="local://wi04-benchmark", license="synthetic fixture", evidence_class="E0")
        indexed = index_repository(root, database, project="benchmark", source=source)
        state = probe_repository_state(root, project="benchmark")
        graph = build_code_graph(database, project="benchmark", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
        started = time.perf_counter()
        built = build_convention_memory(database, project="benchmark", root_id=state.root_id, graph_snapshot_id=graph["graph_snapshot_id"])
        build_ms = (time.perf_counter() - started) * 1_000
        before = _digest(database)
        durations: list[float] = []
        digests: list[str] = []
        for _ in range(max(1, args.iterations)):
            tick = time.perf_counter()
            preview = preview_convention_memory(database, project="benchmark", root_id=state.root_id)
            durations.append((time.perf_counter() - tick) * 1_000)
            digests.append(str(preview["convention_digest"]))
        after = _digest(database)
        p95 = statistics.quantiles(durations, n=20, method="inclusive")[18] if len(durations) >= 2 else durations[0]
        checks = {
            "cards_present": bool(built["cards"]),
            "p95_budget": p95 <= args.p95_budget_ms,
            "deterministic_digest": len(set(digests)) == 1,
            "preview_no_writes": before == after,
            "no_raw_source": all("content" not in card and "raw_source" not in card for card in built["cards"]),
            "proposal_only": all(card["status"] == "proposal" for card in built["cards"]),
        }
        report = {
            "schema_version": "bhm.repository-conventions.benchmark.v1",
            "ok": all(checks.values()),
            "fixture": {"source_files": args.files, "graph_nodes": graph["summary"]["node_count"], "graph_edges": graph["summary"]["edge_count"]},
            "cards": {"count": len(built["cards"]), "kinds": sorted({str(card["card_kind"]) for card in built["cards"]}), "convention_digest": built["convention_digest"]},
            "latency": {"build_ms": round(build_ms, 3), "sample_count": len(durations), "p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3)},
            "checks": checks,
            "sqlite_main_digest_before": before,
            "sqlite_main_digest_after": after,
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
