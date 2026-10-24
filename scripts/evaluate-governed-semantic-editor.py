#!/usr/bin/env python
"""Run the governed semantic editor against synthetic evidence only.

This is an operator-facing quality gate, not a data migration or shadow queue
writer.  It needs an explicitly enabled local semantic editor, calls it only
with the checked-in redacted fixture, and writes only a content-free report
when ``--output`` is supplied.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from blackholememory.governed_semantic_editor import LocalGatewaySemanticCompletion
from blackholememory.governed_semantic_editor import SemanticEditorConfig
from blackholememory.governed_semantic_model_evaluation import evaluate_model_evidence_cases
from blackholememory.governed_semantic_model_evaluation import load_model_evidence_dataset


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPO_ROOT / "tests" / "fixtures" / "governed-semantic-editor-evidence.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a read-only governed semantic model quality gate.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET, help="Synthetic evidence fixture path.")
    parser.add_argument("--output", type=Path, help="Optional content-free JSON receipt path.")
    args = parser.parse_args(argv)

    dataset = load_model_evidence_dataset(args.dataset)
    completion = LocalGatewaySemanticCompletion(SemanticEditorConfig.from_env())
    report = evaluate_model_evidence_cases(dataset, completion)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0 if report["gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
