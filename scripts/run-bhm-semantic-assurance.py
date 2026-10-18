#!/usr/bin/env python
"""Evaluate local content-free semantic-assurance classifier rows offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.semantic_assurance_evaluation import SemanticAssuranceEvaluationError
from blackholememory.semantic_assurance_evaluation import evaluate_semantic_assurance
from blackholememory.semantic_assurance_evaluation import load_semantic_assurance_dataset
from blackholememory.semantic_assurance_evaluation import load_semantic_assurance_predictions
from blackholememory.semantic_assurance_evaluation import write_semantic_assurance_receipt


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPO_ROOT / "tests" / "fixtures" / "semantic-assurance-v1.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET, help="local semantic-assurance JSON fixture")
    parser.add_argument("--predictions", type=Path, required=True, help="local content-free prediction JSON")
    parser.add_argument("--partition", choices=("development", "validation", "sealed_holdout"), default="sealed_holdout")
    parser.add_argument("--model-id", default="not_provided")
    parser.add_argument("--model-variant", default="not_provided")
    parser.add_argument("--prompt-digest", default="not_provided")
    parser.add_argument("--endpoint", default="not_provided")
    parser.add_argument("--output", type=Path, help="optional local content-free receipt JSON path")
    args = parser.parse_args(argv)
    try:
        report = evaluate_semantic_assurance(
            load_semantic_assurance_dataset(args.dataset),
            load_semantic_assurance_predictions(args.predictions),
            partition=args.partition,
            invocation={
                "model_id": args.model_id,
                "model_variant": args.model_variant,
                "prompt_digest": args.prompt_digest,
                "endpoint": args.endpoint,
            },
        )
    except SemanticAssuranceEvaluationError as exc:
        print(json.dumps({"accepted": False, "error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 2
    if args.output:
        write_semantic_assurance_receipt(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["gate"]["mechanical_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
