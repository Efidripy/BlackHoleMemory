#!/usr/bin/env python
"""Run the read-only quality gate against an already-loaded local LM Studio model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.local_model_quality_evaluation import (
    DEFAULT_BASE_URL,
    LocalModelQualityError,
    load_quality_dataset,
    run_quality_evaluation,
    write_quality_receipt,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPO_ROOT / "tests" / "fixtures" / "local_model_quality" / "russian-grounded-v1.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_quality_evaluation(
            load_quality_dataset(args.dataset),
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
            repeats=args.repeats,
        )
    except LocalModelQualityError as exc:
        print(json.dumps({"gate_passed": False, "error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 2
    if args.output:
        write_quality_receipt(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
