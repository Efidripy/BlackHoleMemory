#!/usr/bin/env python
"""Run one local, admitted, content-free LoCoMo lexical smoke receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.external_evaluation_admission import load_external_evaluation_admission_report
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.locomo_smoke import LoCoMoSmokeError
from blackholememory.locomo_smoke import run_locomo_lexical_smoke


def _require_inside(root: Path, candidate: Path, *, label: str) -> Path:
    resolved_root = root.expanduser().resolve()
    resolved_candidate = candidate.expanduser().resolve()
    if not resolved_root.is_dir():
        raise LoCoMoSmokeError("dataset root must be an existing directory")
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise LoCoMoSmokeError(f"{label} must stay inside dataset root") from exc
    return resolved_candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--admission-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-cases", type=int, default=50)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    try:
        dataset_root = args.dataset_root.expanduser().resolve()
        _require_inside(dataset_root, args.dataset, label="dataset")
        admission_path = _require_inside(dataset_root, args.admission_report, label="admission report")
        admission = load_external_evaluation_admission_report(admission_path)
        result = run_locomo_lexical_smoke(
            dataset_root,
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
    except (LoCoMoSmokeError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
