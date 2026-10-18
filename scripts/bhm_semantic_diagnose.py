#!/usr/bin/env python3
"""Diagnostic runner for the checked-in synthetic Governed Semantic Editor fixture.

Run from the repository root:
    uv run python scripts/bhm_semantic_diagnose.py --raw

- Uses only the repository's synthetic fixture, never a real memory database.
- Does not modify BHM source, fixture, thresholds, storage, or application state.
- Shows model's validated JSON decision BEFORE the policy and the proposal AFTER.
- This is diagnosis, not the complete quality gate. Run the official evaluation too.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from blackholememory.governed_semantic_editor import (
    LocalGatewaySemanticCompletion,
    SemanticEditorConfig,
    build_semantic_proposal,
)
from blackholememory.governed_semantic_model_evaluation import (
    _canonical_records,
    _proposal_result,
    load_model_evidence_dataset,
)

FOCUS_CASES = (
    "create-projection-rule",
    "create-operator-approval",
    "no-op-paraphrase-config",
    "conflict-cleanup",
)
FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "governed-semantic-editor-evidence.json"


class CaptureCompletion:
    """Observe the existing completion boundary, without modifying its output."""

    def __init__(self, delegate: LocalGatewaySemanticCompletion) -> None:
        self._delegate = delegate
        self.raw: dict[str, Any] | None = None
        self.calls = 0

    def complete(
        self, *, project: str, query: str, records: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        self.calls += 1
        output = self._delegate.complete(project=project, query=query, records=records)
        self.raw = dict(output)
        return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose BHM semantic editor on synthetic cases only")
    parser.add_argument("--case", action="append", default=[], help="Run one exact case_id (may repeat)")
    parser.add_argument("--all", action="store_true", help="Diagnose all 29 model cases")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat selected scenarios (1..10)")
    parser.add_argument("--raw", action="store_true", help="Print full model JSON for synthetic fixture only")
    parser.add_argument("--input", action="store_true", help="Print synthetic input query and records")
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 10:
        parser.error("--repeat must be between 1 and 10")
    if args.all and args.case:
        parser.error("Choose either --all or --case")

    dataset = load_model_evidence_dataset(FIXTURE)
    cases = {c["case_id"]: c for c in dataset["cases"] if c["kind"] == "model"}
    selected = list(cases) if args.all else (list(dict.fromkeys(args.case)) if args.case else list(FOCUS_CASES))
    missing = sorted(set(selected) - set(cases))
    if missing:
        parser.error("Unknown model scenario(s): " + ", ".join(missing))

    config = SemanticEditorConfig.from_env()
    completion = LocalGatewaySemanticCompletion(config)
    print(f"Model: {config.model_id} | local URL: {config.base_url}", flush=True)
    print(f"Timeout: {config.timeout_seconds}s | max_tokens: {config.max_tokens}", flush=True)
    print("Source: CHECKED-IN SYNTHETIC fixture ONLY; no persistence or apply", flush=True)
    print("Note: private _proposal_result helper is used only for per-case diagnosis.", flush=True)
    n_pass = n_fail = 0

    for iteration in range(1, args.repeat + 1):
        for case_id in selected:
            case = cases[case_id]
            cap = CaptureCompletion(completion)
            print(f"\n[{iteration}/{args.repeat}] {case_id} | expected={case['expected_operation']}", flush=True)
            if args.input:
                print(json.dumps({"query": case["query"], "records": case["records"]},
                                 ensure_ascii=False, indent=2), flush=True)
            started = time.perf_counter()
            try:
                proposal = build_semantic_proposal(
                    project=str(case["project"]),
                    query=str(case["query"]),
                    retrieved_records=_canonical_records(case["records"]),
                    completion=cap,
                )
                result = _proposal_result(case, proposal, model_called=cap.calls > 0)
                raw = cap.raw or {}
                policy = (proposal.get("semantic_editor") or {}).get("policy") or {}
                failed_checks = [name for name, ok in result["checks"].items() if not ok]
                print(json.dumps({
                    "passed": result["passed"],
                    "raw_model_operation": raw.get("operation"),
                    "raw_conflicts_count": len(raw.get("conflicts") or []),
                    "raw_confidence": raw.get("confidence"),
                    "policy_operation": proposal.get("operation"),
                    "policy_decision": policy.get("decision"),
                    "policy_conflicts_present": policy.get("conflicts_present"),
                    "failed_checks": failed_checks,
                    "duration_seconds": round(time.perf_counter() - started, 3),
                }, ensure_ascii=False, indent=2), flush=True)
                if args.raw:
                    print("MODEL_JSON (SYNTHETIC CASE ONLY):", flush=True)
                    print(json.dumps(raw, ensure_ascii=False, indent=2), flush=True)
                n_pass += int(result["passed"])
                n_fail += int(not result["passed"])
            except Exception as exc:
                n_fail += 1
                print(json.dumps({
                    "passed": False,
                    "error_type": type(exc).__name__,
                    "error_code": str(getattr(exc, "code", ""))[:100],
                    "duration_seconds": round(time.perf_counter() - started, 3),
                }, ensure_ascii=False, indent=2), flush=True)

    print(f"\nDiagnostic totals: passed={n_pass}, failed={n_fail}", flush=True)
    print("This is NOT the full quality gate. Run evaluate-governed-semantic-editor.py to certify.")
    return 0 if n_fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
