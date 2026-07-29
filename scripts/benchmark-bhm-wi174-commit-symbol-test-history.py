from __future__ import annotations

import argparse
import hashlib
import json
import time

from blackholememory.git_history_test_receipt import build_commit_symbol_test_history_receipt


HISTORY = {
    "commits_considered": 4,
    "hotspots": [{"path": "src/service.py", "commits": 4}, {"path": "tests/test_service.py", "commits": 3}],
    "cochange": [{"changed_path": "src/service.py", "companion_path": "tests/test_service.py", "commits": 3}],
    "commit_records": [
        {"commit_digest": f"{index:032x}", "file_count": 2, "paths": ["src/service.py", "tests/test_service.py"], "touches_changed_paths": True}
        for index in range(1, 5)
    ],
}
SYMBOLS = [{"relation": "hotspot", "path": "src/service.py", "stable_key": "fn:service", "node_kind": "function", "qualified_name": "service", "commits": 4}]
TESTS = [{"path": "tests/test_service.py", "stable_key": "test:service", "node_kind": "test", "qualified_name": "test_service"}]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    if args.iterations < 1:
        raise SystemExit("iterations must be positive")
    first = build_commit_symbol_test_history_receipt(HISTORY, SYMBOLS, TESTS, changed_paths=["src/service.py"])
    timings: list[float] = []
    last = first
    for _ in range(args.iterations):
        start = time.perf_counter()
        last = build_commit_symbol_test_history_receipt(HISTORY, SYMBOLS, TESTS, changed_paths=["src/service.py"])
        timings.append((time.perf_counter() - start) * 1000)
    elapsed_ms = sum(timings)
    ordered = sorted(timings)
    def percentile(fraction: float) -> float:
        return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]
    repeat = build_commit_symbol_test_history_receipt(HISTORY, SYMBOLS, TESTS, changed_paths=["src/service.py"])
    fixture_digest = hashlib.sha256(json.dumps(HISTORY, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    output = {
        "ok": first == repeat and first["status"] == "pass",
        "iterations": args.iterations,
        "elapsed_ms": round(elapsed_ms, 6),
        "p50_ms": round(percentile(0.50), 6),
        "p95_ms": round(percentile(0.95), 6),
        "max_ms": round(max(ordered), 6),
        "schema_version": first["schema_version"],
        "receipt_digest": last["receipt_digest"],
        "fixture_digest": fixture_digest,
        "commit_records": first["counts"]["commit_records"],
        "symbol_correlations": first["counts"]["symbol_correlations"],
        "test_correlations": first["counts"]["test_correlations"],
        "commit_test_links": first["counts"]["commit_test_links"],
        "raw_source": first["provenance"]["raw_source_returned"],
        "writes": first["execution"]["writes_sqlite_state"] or first["execution"]["writes_qdrant"],
    }
    print(json.dumps(output, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
