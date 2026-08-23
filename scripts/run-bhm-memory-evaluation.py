"""Run one bounded, offline BHM-owned memory-evaluation fixture.

The runner only evaluates already-recorded receipts. It never downloads a
dataset, calls a model, starts BHM, or mutates SQLite/Qdrant/Mem0.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.memory_evaluation import FrozenEvaluationFixtureError
from blackholememory.memory_evaluation import run_frozen_evaluation_fixture


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate one BHM-owned frozen memory fixture without network or model calls.")
    parser.add_argument("--fixture", required=True, type=Path, help="Path to a BHM-owned frozen fixture JSON file.")
    parser.add_argument("--k", default=5, type=int, help="Bounded retrieval cutoff (1..50).")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        report = run_frozen_evaluation_fixture(args.fixture, k=args.k)
    except (FrozenEvaluationFixtureError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "report": report}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
