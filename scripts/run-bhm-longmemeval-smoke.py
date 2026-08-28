#!/usr/bin/env python
"""Run one local, admitted, content-free LongMemEval-S lexical smoke receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.external_evaluation_admission import load_external_evaluation_admission_report
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.longmemeval_smoke import LongMemEvalSmokeError
from blackholememory.longmemeval_smoke import run_longmemeval_lexical_smoke


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--admission-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-cases", type=int, default=50)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    try:
        admission = load_external_evaluation_admission_report(args.admission_report)
        result = run_longmemeval_lexical_smoke(
            args.dataset,
            dataset_version=args.dataset_version,
            admission_report=admission,
            max_cases=args.max_cases,
            k=args.k,
        )
        output_dir = args.output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        for name, value in result.items():
            payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            replace_bytes_safely(output_dir / f"{name}.json", payload.encode("utf-8"))
        print(json.dumps({"ok": True, "report": result["report"]}, ensure_ascii=False, sort_keys=True))
        return 0
    except (LongMemEvalSmokeError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
