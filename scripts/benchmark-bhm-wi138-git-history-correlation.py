#!/usr/bin/env python
"""Benchmark deterministic WI-138 history-correlation receipts."""

from __future__ import annotations

import argparse
import time

from blackholememory.change_impact import build_git_history_correlation_receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    if args.iterations < 1 or args.iterations > 10_000:
        raise SystemExit("iterations must be between 1 and 10000")
    history = {
        "commits_considered": 32,
        "hotspots": [{"path": "src/service.py", "commits": 12}],
        "cochange": [{"changed_path": "src/service.py", "companion_path": "tests/service.py", "commits": 8}],
    }
    symbols = [{"relation": "cochange", "path": "tests/service.py", "stable_key": "test:service"}]
    started = time.perf_counter()
    digests = [
        build_git_history_correlation_receipt(history, symbols, changed_paths=["src/service.py"])["receipt_digest"]
        for _ in range(args.iterations)
    ]
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    print(
        {
            "iterations": args.iterations,
            "total_ms": round(elapsed_ms, 3),
            "per_call_ms": round(elapsed_ms / args.iterations, 6),
            "stable_digest": len(set(digests)) == 1,
            "receipt_digest": digests[0],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
