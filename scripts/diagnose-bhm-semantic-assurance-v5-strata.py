#!/usr/bin/env python
"""Diagnose owner-only V5 sealed-label coverage without altering evidence.

The command accepts a local normalized semantic-assurance dataset and its
curator-audited V4/V5 human-label manifest.  Its output is content-free: it
contains only fixed-label aggregate populations, deficits, and bindings.  It
never calls a model, contacts a service, opens BHM storage, or authorises a
lifecycle action.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.semantic_assurance_evaluation import SemanticAssuranceEvaluationError
from blackholememory.semantic_assurance_evaluation import load_semantic_assurance_dataset
from blackholememory.semantic_assurance_label_admission import SemanticAssuranceLabelAdmissionError
from blackholememory.semantic_assurance_label_admission import assess_semantic_assurance_v5_readiness_strata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True, help="owner-only final dataset JSON")
    parser.add_argument("--manifest", type=Path, required=True, help="owner-only V4 or V5 label manifest JSON")
    parser.add_argument("--output", type=Path, help="optional content-free diagnostic receipt JSON")
    args = parser.parse_args(argv)
    try:
        dataset = load_semantic_assurance_dataset(args.dataset)
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        receipt = assess_semantic_assurance_v5_readiness_strata(manifest, dataset)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        SemanticAssuranceEvaluationError,
        SemanticAssuranceLabelAdmissionError,
    ) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 2
    if args.output:
        if args.output.suffix.casefold() != ".json":
            parser.error("--output must end in .json")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["readiness_eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
