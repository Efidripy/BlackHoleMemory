#!/usr/bin/env python
"""Validate one local, pinned external evaluation dataset admission manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.external_evaluation_admission import ExternalEvaluationAdmissionError
from blackholememory.external_evaluation_admission import validate_external_evaluation_dataset_admission
from blackholememory.filesystem_boundaries import replace_bytes_safely


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = validate_external_evaluation_dataset_admission(args.dataset_root, args.manifest)
    except ExternalEvaluationAdmissionError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report is not None:
        replace_bytes_safely(args.report.expanduser().resolve(), (rendered + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
