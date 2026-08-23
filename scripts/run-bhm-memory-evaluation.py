"""Run one bounded, offline recorded memory-evaluation replay.

The runner evaluates only already-recorded, content-free manifests and
retrieval receipts. It never downloads a dataset, calls a model, starts BHM,
or mutates SQLite/Qdrant/Mem0. Named external suites additionally require a
matching local-only admission report.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.memory_evaluation import FrozenEvaluationFixtureError
from blackholememory.memory_evaluation import EvaluationAdmissionBindingError
from blackholememory.memory_evaluation import evaluate_retrieval
from blackholememory.memory_evaluation import load_recorded_evaluation_manifest
from blackholememory.memory_evaluation import load_recorded_retrieval_receipts
from blackholememory.memory_evaluation import run_frozen_evaluation_fixture
from blackholememory.external_evaluation_admission import ExternalEvaluationAdmissionError
from blackholememory.external_evaluation_admission import load_external_evaluation_admission_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate one bounded recorded memory replay without network or model calls.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", type=Path, help="Path to a BHM-owned frozen fixture JSON file.")
    source.add_argument("--manifest", type=Path, help="Content-free recorded manifest for a BHM or approved external replay.")
    parser.add_argument("--receipts", type=Path, help="Content-free recorded retrieval receipts; required with --manifest.")
    parser.add_argument("--admission-report", type=Path, help="Verified local-only admission report; required for locomo/longmemeval.")
    parser.add_argument("--k", default=5, type=int, help="Bounded retrieval cutoff (1..50).")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.fixture is not None:
            if args.receipts is not None or args.admission_report is not None:
                raise FrozenEvaluationFixtureError("--fixture cannot be combined with --receipts or --admission-report")
            report = run_frozen_evaluation_fixture(args.fixture, k=args.k)
        else:
            if args.receipts is None:
                raise FrozenEvaluationFixtureError("--manifest requires --receipts")
            manifest = load_recorded_evaluation_manifest(args.manifest)
            receipts = load_recorded_retrieval_receipts(args.receipts)
            admission = load_external_evaluation_admission_report(args.admission_report) if args.admission_report is not None else None
            report = evaluate_retrieval(manifest, receipts, k=args.k, admission_report=admission)
    except (EvaluationAdmissionBindingError, ExternalEvaluationAdmissionError, FrozenEvaluationFixtureError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "report": report}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
