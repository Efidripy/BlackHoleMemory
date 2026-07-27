"""Bounded multi-candidate proposals with evidence-first deterministic judging.

This module creates independent architect/coder/tester/critic candidate specs,
validates bounded candidate results and ranks them using explicit evidence.
Agreement is reported for observability only: consensus without validator
evidence is never treated as correctness and no result gains authority.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .llm_safety import PROPOSAL_AUTHORITY
from .llm_safety import build_proposal_envelope
from .llm_safety import sanitize_llm_value


LLM_CANDIDATE_SCHEMA_VERSION = "bhm.llm.candidates.v1"
LLM_CANDIDATE_JUDGE_VERSION = "bhm.llm.candidate-judge.v1"
LLM_CANDIDATE_MAX = 8
LLM_CANDIDATE_MAX_ROLES = 4
LLM_CANDIDATE_MAX_OBJECTIVE_BYTES = 32 * 1024
LLM_CANDIDATE_MAX_RESULT_BYTES = 64 * 1024
LLM_CANDIDATE_MAX_EVIDENCE_ITEMS = 16
LLM_CANDIDATE_MAX_SOURCE_REFS = 16
LLM_CANDIDATE_ROLES = ("architect", "coder", "tester", "critic")


class CandidateError(ValueError):
    """Base error for candidate planning and judging."""


class CandidateBoundsError(CandidateError):
    pass


class CandidateCollision(CandidateError):
    pass


@dataclass(frozen=True)
class CandidatePlan:
    plan_id: str
    task_id: str
    project: str
    objective_digest: str
    candidates: tuple[dict[str, Any], ...]
    plan_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LLM_CANDIDATE_SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "project": self.project,
            "objective_digest": self.objective_digest,
            "candidate_count": len(self.candidates),
            "roles": sorted({str(candidate["role"]) for candidate in self.candidates}),
            "candidates": [dict(candidate) for candidate in self.candidates],
            "plan_digest": self.plan_digest,
            "judge": {
                "version": LLM_CANDIDATE_JUDGE_VERSION,
                "evidence_first": True,
                "consensus_is_correctness": False,
            },
            "authority": PROPOSAL_AUTHORITY,
            "auto_apply": False,
            "execution_enabled": False,
        }


def build_candidate_plan(
    task_id: str,
    objective: Any,
    *,
    project: str = "blackholememory",
    roles: Sequence[str] | None = None,
    candidate_count: int | None = None,
    prompt_version: str = "candidate-v1",
    model_digest: str = "local-model",
) -> CandidatePlan:
    """Create deterministic independent candidate specs from a bounded objective."""

    normalized_task = str(task_id or "").strip()
    if not normalized_task:
        raise CandidateError("task_id is required")
    normalized_project = str(project or "blackholememory").strip() or "blackholememory"
    safe_objective = sanitize_llm_value(
        objective,
        source="llm-candidate-objective",
        project=normalized_project,
        max_input_bytes=LLM_CANDIDATE_MAX_OBJECTIVE_BYTES,
    ).value
    objective_json = _canonical_json(safe_objective)
    if len(objective_json.encode("utf-8")) > LLM_CANDIDATE_MAX_OBJECTIVE_BYTES:
        raise CandidateBoundsError("candidate objective exceeds byte limit")
    objective_digest = _sha256(objective_json)
    selected_roles = _normalize_roles(roles)
    requested_count = len(selected_roles) if candidate_count is None else int(candidate_count)
    if requested_count < 1 or requested_count > LLM_CANDIDATE_MAX:
        raise CandidateBoundsError(f"candidate_count must be between 1 and {LLM_CANDIDATE_MAX}")
    if requested_count < len(selected_roles):
        selected_roles = selected_roles[:requested_count]
    while len(selected_roles) < requested_count:
        selected_roles.append(LLM_CANDIDATE_ROLES[len(selected_roles) % len(LLM_CANDIDATE_ROLES)])

    plan_id = f"candidate_plan_{_sha256(f'{normalized_task}:{objective_digest}')[:32]}"
    candidates: list[dict[str, Any]] = []
    for ordinal, role in enumerate(selected_roles):
        candidate_id = f"candidate_{_sha256(f'{plan_id}:{role}:{ordinal}')[:32]}"
        candidates.append(
            {
                "candidate_id": candidate_id,
                "plan_id": plan_id,
                "task_id": normalized_task,
                "project": normalized_project,
                "role": role,
                "ordinal": ordinal,
                "objective_digest": objective_digest,
                "prompt_version": str(prompt_version or "candidate-v1")[:120],
                "model_digest": str(model_digest or "local-model")[:160],
                "independent": True,
                "authority": PROPOSAL_AUTHORITY,
                "auto_apply": False,
            }
        )
    core = {
        "schema_version": LLM_CANDIDATE_SCHEMA_VERSION,
        "plan_id": plan_id,
        "task_id": normalized_task,
        "project": normalized_project,
        "objective_digest": objective_digest,
        "candidates": candidates,
    }
    return CandidatePlan(
        plan_id=plan_id,
        task_id=normalized_task,
        project=normalized_project,
        objective_digest=objective_digest,
        candidates=tuple(candidates),
        plan_digest=_sha256(_canonical_json(core)),
    )


def evaluate_candidate(
    candidate: Mapping[str, Any],
    output: Any,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one candidate using only bounded deterministic evidence."""

    candidate_id = str(candidate.get("candidate_id") or "").strip()
    role = str(candidate.get("role") or "").strip().casefold()
    if not candidate_id or role not in LLM_CANDIDATE_ROLES:
        raise CandidateError("candidate spec must contain a known role and candidate_id")
    safe_output = sanitize_llm_value(output, source="llm-candidate-output", project=str(candidate.get("project") or "blackholememory")).value
    output_json = _canonical_json(safe_output)
    if len(output_json.encode("utf-8")) > LLM_CANDIDATE_MAX_RESULT_BYTES:
        raise CandidateBoundsError("candidate output exceeds byte limit")
    safe_evidence = sanitize_llm_value(
        dict(evidence or {}),
        source="llm-candidate-evidence",
        project=str(candidate.get("project") or "blackholememory"),
    ).value
    if not isinstance(safe_evidence, dict):
        raise CandidateError("candidate evidence must be an object")
    validator_items = safe_evidence.get("validators") if isinstance(safe_evidence.get("validators"), list) else []
    validator_items = validator_items[:LLM_CANDIDATE_MAX_EVIDENCE_ITEMS]
    validator_pass = bool(validator_items) and all(
        isinstance(item, Mapping) and item.get("passed") is True for item in validator_items
    )
    schema_valid = safe_evidence.get("schema_valid") is True
    leakage_free = safe_evidence.get("leakage_free") is True
    mutation_free = safe_evidence.get("mutation_free") is True
    source_refs = _bounded_strings(safe_evidence.get("source_refs"), LLM_CANDIDATE_MAX_SOURCE_REFS)
    evidence_present = bool(source_refs) or bool(safe_evidence.get("evidence_digest"))
    components = {
        "schema": 0.25 if schema_valid else 0.0,
        "validators": 0.35 if validator_pass else 0.0,
        "provenance": 0.20 if evidence_present else 0.0,
        "safety": 0.20 if leakage_free and mutation_free else 0.0,
    }
    score = round(sum(components.values()), 6)
    validated = bool(schema_valid and validator_pass and evidence_present and leakage_free and mutation_free)
    evidence_summary = {
        "schema_valid": schema_valid,
        "validator_count": len(validator_items),
        "validator_pass": validator_pass,
        "source_ref_count": len(source_refs),
        "leakage_free": leakage_free,
        "mutation_free": mutation_free,
        "evidence_digest": str(safe_evidence.get("evidence_digest") or "")[:128],
    }
    evidence_digest = _sha256(_canonical_json(evidence_summary))
    proposal = build_proposal_envelope(
        job_id=candidate_id,
        output=safe_output,
        provenance={
            "source": "llm-multi-candidate",
            "project": str(candidate.get("project") or "blackholememory"),
            "role": role,
            "objective_digest": str(candidate.get("objective_digest") or "")[:128],
            "evidence_digest": evidence_digest,
        },
    )
    return {
        "schema_version": LLM_CANDIDATE_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "plan_id": str(candidate.get("plan_id") or ""),
        "role": role,
        "ordinal": int(candidate.get("ordinal") or 0),
        "output_digest": _sha256(output_json),
        "proposal": proposal,
        "evidence": evidence_summary,
        "evidence_digest": evidence_digest,
        "score": score,
        "status": "validated" if validated else "insufficient_evidence",
        "authority": PROPOSAL_AUTHORITY,
        "auto_apply": False,
        "requires_approval": True,
    }


def judge_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    min_validated: int = 1,
) -> dict[str, Any]:
    """Rank candidates by evidence; expose consensus without trusting it."""

    rows = [dict(candidate) for candidate in candidates[:LLM_CANDIDATE_MAX]]
    if not rows:
        raise CandidateError("at least one candidate result is required")
    validated = [row for row in rows if row.get("status") == "validated"]
    ordered = sorted(
        rows,
        key=lambda row: (
            row.get("status") == "validated",
            float(row.get("score") or 0.0),
            bool((row.get("evidence") or {}).get("validator_pass")),
            int((row.get("evidence") or {}).get("source_ref_count") or 0),
            str(row.get("candidate_id") or ""),
        ),
        reverse=True,
    )
    digest_counts = Counter(str(row.get("output_digest") or "") for row in rows)
    consensus = sorted(
        ({"output_digest": digest, "count": count} for digest, count in digest_counts.items() if digest),
        key=lambda row: (-int(row["count"]), str(row["output_digest"])),
    )[:LLM_CANDIDATE_MAX]
    winner = ordered[0] if len(validated) >= max(int(min_validated), 1) and ordered[0].get("status") == "validated" else None
    correctness = "validated_by_evidence" if winner is not None else "insufficient_evidence"
    plan_id = str(rows[0].get("plan_id") or "")
    decision_core = {
        "schema_version": LLM_CANDIDATE_JUDGE_VERSION,
        "plan_id": plan_id,
        "candidate_ids": sorted(str(row.get("candidate_id") or "") for row in rows),
        "winner_id": str(winner.get("candidate_id")) if winner else None,
        "correctness": correctness,
        "consensus": consensus,
    }
    return {
        **decision_core,
        "judge_digest": _sha256(_canonical_json(decision_core)),
        "ranked_candidate_ids": [str(row.get("candidate_id") or "") for row in ordered],
        "validated_count": len(validated),
        "consensus_is_correctness": False,
        "winner": winner,
        "authority": PROPOSAL_AUTHORITY,
        "auto_apply": False,
        "requires_approval": True,
    }


def _normalize_roles(roles: Sequence[str] | None) -> list[str]:
    values = list(roles or LLM_CANDIDATE_ROLES)
    if len(values) > LLM_CANDIDATE_MAX_ROLES:
        raise CandidateBoundsError(f"at most {LLM_CANDIDATE_MAX_ROLES} roles are supported")
    result: list[str] = []
    for value in values:
        role = str(value or "").strip().casefold()
        if role not in LLM_CANDIDATE_ROLES:
            raise CandidateError(f"unsupported candidate role: {value}")
        if role not in result:
            result.append(role)
    return result or list(LLM_CANDIDATE_ROLES)


def _bounded_strings(value: Any, limit: int) -> list[str]:
    values = [value] if isinstance(value, str) else list(value or []) if isinstance(value, (list, tuple, set, frozenset)) else []
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()[:240]
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str, allow_nan=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


__all__ = [
    "LLM_CANDIDATE_JUDGE_VERSION",
    "LLM_CANDIDATE_MAX",
    "LLM_CANDIDATE_ROLES",
    "LLM_CANDIDATE_SCHEMA_VERSION",
    "CandidateBoundsError",
    "CandidateCollision",
    "CandidateError",
    "CandidatePlan",
    "build_candidate_plan",
    "evaluate_candidate",
    "judge_candidates",
]
