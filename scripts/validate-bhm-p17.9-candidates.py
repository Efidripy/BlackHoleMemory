"""Deterministic offline gate for the P17.9 evidence-first candidate judge."""

from __future__ import annotations

import json

from blackholememory.llm_candidates import build_candidate_plan
from blackholememory.llm_candidates import evaluate_candidate
from blackholememory.llm_candidates import judge_candidates


def _evidence(valid: bool = True) -> dict:
    return {
        "schema_valid": valid,
        "validators": [{"name": "deterministic-validator", "passed": valid}],
        "source_refs": [
            ".docs/archive/2026-08-21-pre-consolidation/plan/"
            "bhm-only-cutover-master-plan.md"
        ]
        if valid
        else [],
        "leakage_free": valid,
        "mutation_free": valid,
        "evidence_digest": "p17.9-evidence" if valid else "",
    }


def main() -> int:
    plan = build_candidate_plan(
        "p17.9-validator",
        {"goal": "select an evidence-backed proposal"},
        candidate_count=4,
    )
    valid = evaluate_candidate(plan.candidates[0], {"answer": "validated"}, _evidence(True))
    invalid = [
        evaluate_candidate(candidate, {"answer": "popular"}, _evidence(False))
        for candidate in plan.candidates[1:]
    ]
    invalid[1]["output_digest"] = invalid[0]["output_digest"]
    invalid[2]["output_digest"] = invalid[0]["output_digest"]
    decision = judge_candidates([valid, *invalid])
    checks = {
        "plan_stable": plan.plan_digest == build_candidate_plan(
            "p17.9-validator", {"goal": "select an evidence-backed proposal"}, candidate_count=4
        ).plan_digest,
        "four_roles": len(plan.candidates) == 4,
        "validated_winner": decision["winner"]["candidate_id"] == valid["candidate_id"],
        "consensus_not_correctness": decision["consensus"][0]["count"] == 3
        and decision["consensus_is_correctness"] is False,
        "proposal_only": decision["authority"] == "proposal" and decision["auto_apply"] is False,
        "evidence_gate": decision["correctness"] == "validated_by_evidence",
    }
    report = {
        "ok": all(checks.values()),
        "schema_version": plan.as_dict()["schema_version"],
        "judge_version": decision["schema_version"],
        "plan_id": plan.plan_id,
        "winner_id": decision["winner"]["candidate_id"] if decision["winner"] else None,
        "validated_count": decision["validated_count"],
        "consensus": decision["consensus"],
        "checks": checks,
        "auto_apply": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
