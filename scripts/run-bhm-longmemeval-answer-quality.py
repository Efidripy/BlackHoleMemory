#!/usr/bin/env python
"""Emit a content-free deterministic LongMemEval answer-quality receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from blackholememory.external_evaluation_admission import load_external_evaluation_admission_report
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.filesystem_boundaries import read_bytes_safely
from blackholememory.longmemeval_answer import LongMemEvalAnswerQualityError
from blackholememory.longmemeval_answer import evaluate_longmemeval_answer_quality


_MAX_GENERATED_ANSWERS_BYTES = 512 * 1024


def _load_generated_answers(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    try:
        payload: Any = json.loads(read_bytes_safely(path.expanduser().resolve(), max_bytes=_MAX_GENERATED_ANSWERS_BYTES))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LongMemEvalAnswerQualityError("generated answers must be bounded UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise LongMemEvalAnswerQualityError("generated answers JSON root must be an object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--admission-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generated-answers", type=Path, default=None)
    parser.add_argument("--max-cases", type=int, default=50)
    parser.add_argument("--case-split-index", type=int, default=0)
    args = parser.parse_args()
    try:
        result = evaluate_longmemeval_answer_quality(
            args.dataset,
            dataset_version=args.dataset_version,
            admission_report=load_external_evaluation_admission_report(args.admission_report),
            generated_answers=_load_generated_answers(args.generated_answers),
            max_cases=args.max_cases,
            split_index=args.case_split_index,
        )
        replace_bytes_safely(
            args.output.expanduser().resolve(),
            (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        print(json.dumps({"ok": True, "answer_quality": result["answer_quality"], "report_digest": result["report_digest"]}, sort_keys=True))
        return 0
    except (LongMemEvalAnswerQualityError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
