"""Deterministic offline gate for the P17.11 local-first delegation policy."""

from __future__ import annotations

import json

from blackholememory.llm_delegation_policy import DelegationPolicyError
from blackholememory.llm_delegation_policy import decide_delegation
from blackholememory.llm_delegation_policy import delegation_policy_snapshot


def main() -> int:
    local = decide_delegation("summarization", confidence=0.95, local_capabilities=["summarization"])
    codex = decide_delegation("architecture", confidence=0.95, local_capabilities=["summarization"])
    operator = decide_delegation("destructive", confidence=0.95)
    low = decide_delegation("candidate_generation", confidence=0.2, local_capabilities=["candidate_generation"])
    unknown_rejected = False
    try:
        decide_delegation("unknown", confidence=0.95)
    except DelegationPolicyError:
        unknown_rejected = True
    snapshot = delegation_policy_snapshot()
    checks = {
        "local_bulk_is_local": local.destination == "local",
        "architecture_escalates_codex": codex.destination == "codex" and codex.approval_required,
        "destructive_is_operator": operator.destination == "operator" and operator.approval_required,
        "low_confidence_escalates": low.destination == "codex" and "low_confidence_escalation" in low.reason_codes,
        "unknown_fail_closed": unknown_rejected,
        "proposal_only": snapshot["mutation_auto_apply"] is False and snapshot["execution_enabled"] is False,
        "consensus_not_correctness": snapshot["consensus_is_correctness"] is False,
    }
    report = {
        "ok": all(checks.values()),
        "schema_version": snapshot["schema_version"],
        "destinations": {"local": local.destination, "architecture": codex.destination, "destructive": operator.destination, "low_confidence": low.destination},
        "checks": checks,
        "execution_enabled": False,
        "auto_apply": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
