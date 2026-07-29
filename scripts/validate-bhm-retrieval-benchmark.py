#!/usr/bin/env python
"""Run the offline retrieval/context benchmark without writing live state."""

from __future__ import annotations

# The script adds the repository's src directory before importing project modules.
# ruff: noqa: E402

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory import app as bhm_app
from blackholememory.retrieval_benchmark import build_default_benchmark_cases
from blackholememory.retrieval_benchmark import evaluate_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=120)
    parser.add_argument("--token-budget", type=int, default=240)
    parser.add_argument("--include-case-reports", action="store_true")
    args = parser.parse_args()
    cases = build_default_benchmark_cases(args.cases)
    report = evaluate_benchmark(
        cases,
        ranker=bhm_app._rank_hybrid_vector_hits,
        token_budget=args.token_budget,
        include_case_reports=args.include_case_reports,
    )
    report["mode"] = "offline-synthetic"
    report["writes_live_state"] = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
