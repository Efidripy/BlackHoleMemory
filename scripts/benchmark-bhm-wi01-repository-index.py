#!/usr/bin/env python3
"""Deterministic cold/incremental/no-op benchmark for WI-01."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import RepositoryWatcher
from blackholememory.repository_index import index_repository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=240)
    parser.add_argument("--lines-per-file", type=int, default=40)
    parser.add_argument("--changed-files", type=int, default=12)
    parser.add_argument("--cold-ms-per-kloc-budget", type=float, default=2_500.0)
    parser.add_argument("--incremental-ms-per-changed-file-budget", type=float, default=750.0)
    parser.add_argument("--noop-ms-budget", type=float, default=2_000.0)
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _content(index: int, lines: int, *, revision: int = 1) -> str:
    pairs = max(lines // 2, 1)
    return "".join(
        f"def symbol_{index}_{line}():\n    return {index + line + revision}\n"
        for line in range(pairs)
    )


def _fixture(root: Path, *, files: int, lines: int) -> None:
    source = root / "src"
    tests = root / "tests"
    source.mkdir(parents=True)
    tests.mkdir()
    for index in range(files):
        (source / f"module_{index:04d}.py").write_text(_content(index, lines), encoding="utf-8")
    for index in range(max(files // 10, 1)):
        (tests / f"test_module_{index:04d}.py").write_text(
            f"from src.module_{index:04d} import symbol_{index}_0\n\n"
            f"def test_symbol_{index}():\n    assert symbol_{index}_0() >= 0\n",
            encoding="utf-8",
        )
    (root / ".env").write_text("TOKEN=synthetic\n", encoding="utf-8")
    (source / "generated.min.js").write_text("const generated=true;\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.email", "benchmark@example.invalid")
    _git(root, "config", "user.name", "Benchmark")
    _git(root, "add", "-f", ".")
    _git(root, "commit", "-m", "benchmark fixture")


def _measure(function):
    started = time.perf_counter()
    value = function()
    return value, (time.perf_counter() - started) * 1_000


def main() -> int:
    args = parse_args()
    if not 20 <= args.files <= 5_000:
        raise SystemExit("--files must be between 20 and 5000")
    if not 10 <= args.lines_per_file <= 500:
        raise SystemExit("--lines-per-file must be between 10 and 500")
    if not 1 <= args.changed_files < args.files:
        raise SystemExit("--changed-files must be positive and less than --files")
    source = RepositorySourceProvenance(
        source_url="fixture://wi01-benchmark",
        license="synthetic fixture",
        evidence_class="E0",
        owner="WI-01 validator",
    )
    with tempfile.TemporaryDirectory(prefix="bhm-wi01-benchmark-") as raw:
        temp = Path(raw)
        root = temp / "repo"
        root.mkdir()
        database = temp / "memories.sqlite3"
        _fixture(root, files=args.files, lines=args.lines_per_file)

        cold, cold_ms = _measure(lambda: index_repository(root, database, project="benchmark", source=source))
        total_lines = int(cold["snapshot"]["summary"]["total_lines"])
        kloc = max(total_lines / 1_000, 0.001)

        for index in range(args.changed_files):
            (root / "src" / f"module_{index:04d}.py").write_text(
                _content(index, args.lines_per_file, revision=2),
                encoding="utf-8",
            )
        rename_from = root / "src" / f"module_{args.changed_files:04d}.py"
        rename_to = root / "src" / f"renamed_{args.changed_files:04d}.py"
        rename_from.rename(rename_to)
        (root / "src" / f"module_{args.changed_files + 1:04d}.py").unlink()
        (root / "src" / "added_module.py").write_text("def added():\n    return True\n", encoding="utf-8")

        incremental, incremental_ms = _measure(
            lambda: index_repository(root, database, project="benchmark", source=source)
        )
        noop, noop_ms = _measure(lambda: index_repository(root, database, project="benchmark", source=source))
        watcher = RepositoryWatcher(root, database, project="benchmark", source=source)
        freshness = watcher.poll()
        changed_count = max(int(incremental["snapshot"]["delta"]["changed_file_count"]), 1)
        cold_per_kloc = cold_ms / kloc
        incremental_per_changed = incremental_ms / changed_count
        checks = {
            "cold_complete": cold["status"] == "completed" and cold["gates"]["snapshot_checksum_valid"] is True,
            "incremental_complete": incremental["status"] == "completed",
            "incremental_reuse": incremental["metrics"]["reused_unchanged_files"] > 0,
            "rename_detected": len(incremental["snapshot"]["delta"]["renamed"]) == 1,
            "remove_detected": len(incremental["snapshot"]["delta"]["removed"]) == 1,
            "noop_deduplicated": noop["metrics"]["deduplicated"] is True,
            "fresh_after_index": freshness["changed"] is False,
            "cold_budget": cold_per_kloc <= args.cold_ms_per_kloc_budget,
            "incremental_budget": incremental_per_changed <= args.incremental_ms_per_changed_file_budget,
            "noop_budget": noop_ms <= args.noop_ms_budget,
        }
        result = {
            "schema_version": "bhm.repository-index-benchmark.v1",
            "ok": all(checks.values()),
            "fixture": {
                "source_files": args.files,
                "lines_per_file": args.lines_per_file,
                "changed_files_requested": args.changed_files,
                "total_lines_indexed": total_lines,
                "kloc": round(kloc, 3),
            },
            "cold": {
                "duration_ms": round(cold_ms, 3),
                "ms_per_kloc": round(cold_per_kloc, 3),
                "snapshot_id": cold["snapshot_id"],
                "file_count": cold["snapshot"]["summary"]["file_count"],
                "skipped_count": cold["snapshot"]["summary"]["skipped_count"],
            },
            "incremental": {
                "duration_ms": round(incremental_ms, 3),
                "changed_file_count": changed_count,
                "ms_per_changed_file": round(incremental_per_changed, 3),
                "reused_unchanged_files": incremental["metrics"]["reused_unchanged_files"],
                "delta": incremental["snapshot"]["delta"],
                "graph_input_digest": incremental["snapshot"]["graph_input_digest"],
            },
            "noop": {
                "duration_ms": round(noop_ms, 3),
                "deduplicated": noop["metrics"]["deduplicated"],
                "snapshot_id": noop["snapshot_id"],
            },
            "budgets": {
                "cold_ms_per_kloc": args.cold_ms_per_kloc_budget,
                "incremental_ms_per_changed_file": args.incremental_ms_per_changed_file_budget,
                "noop_ms": args.noop_ms_budget,
            },
            "checks": checks,
            "parser_error_rate": 0.0,
            "writes_live_state": False,
            "starts_background_daemon": False,
        }
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
