#!/usr/bin/env python
"""Assess the already-loaded local LM Studio model through semantic assurance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.semantic_assurance_live import DEFAULT_BASE_URL
from blackholememory.semantic_assurance_live import DEFAULT_MODEL_ID
from blackholememory.semantic_assurance_live import DEFAULT_MODEL_VARIANT
from blackholememory.semantic_assurance_live import SemanticAssuranceLiveError
from blackholememory.semantic_assurance_live import run_live_semantic_assurance
from blackholememory.semantic_assurance_live import write_live_semantic_assurance_receipt


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPO_ROOT / "tests" / "fixtures" / "semantic-assurance-v1.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--partition", choices=("development", "validation", "sealed_holdout"), default="sealed_holdout")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--repeats", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--model-variant", default=DEFAULT_MODEL_VARIANT)
    parser.add_argument("--output", type=Path, help="optional explicit content-free JSON receipt path")
    args = parser.parse_args(argv)
    try:
        report = run_live_semantic_assurance(
            args.dataset,
            partition=args.partition,
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
            repeats=args.repeats,
            model_id=args.model_id,
            model_variant=args.model_variant,
        )
    except SemanticAssuranceLiveError as exc:
        print(json.dumps({"accepted": False, "error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 2
    if args.output:
        write_live_semantic_assurance_receipt(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["gate"]["mechanical_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
