#!/usr/bin/env python
"""Run the offline, synthetic BHM context-tier validation contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.context_tier_validation import build_context_tier_validation_report
from blackholememory.filesystem_boundaries import replace_bytes_safely


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=32)
    parser.add_argument("--p95-budget-ms", type=float, default=100.0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = build_context_tier_validation_report(iterations=args.iterations, p95_budget_ms=args.p95_budget_ms)
    except ValueError as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report is not None:
        replace_bytes_safely(args.report.expanduser().resolve(), (rendered + "\n").encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
