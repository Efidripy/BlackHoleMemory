"""Offline validator for the non-mutating hierarchical context-tier contract."""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from typing import Any

from .context_tier_lifecycle import build_context_tier_lifecycle_receipt
from .context_tiers import TierBudget
from .context_tiers import TieredContextItem
from .context_tiers import compile_tiered_context


SCHEMA_VERSION = "bhm.context-tier-validation.v1"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fixture() -> tuple[tuple[TieredContextItem, ...], TierBudget]:
    """Return only synthetic values; no live memory is ever read by this module."""

    items = (
        TieredContextItem(memory_id="tier-working", tier="working", text="working evidence is deliberately longer than its tier budget", source_digest=_digest("working"), priority=10),
        TieredContextItem(memory_id="tier-session", tier="session", text="session evidence is deliberately longer than its tier budget", source_digest=_digest("session"), priority=8),
        TieredContextItem(memory_id="tier-project", tier="project", text="project evidence is deliberately longer than its tier budget", source_digest=_digest("project"), priority=6),
        TieredContextItem(memory_id="tier-archival", tier="archival", text="archival evidence is deliberately longer than its tier budget", source_digest=_digest("archival"), priority=4),
        TieredContextItem(memory_id="tier-working", tier="session", text="duplicate must be omitted", source_digest=_digest("duplicate")),
    )
    return items, TierBudget(working_bytes=12, session_bytes=12, project_bytes=12, archival_bytes=12)


def build_context_tier_validation_report(*, iterations: int = 32, p95_budget_ms: float = 100.0) -> dict[str, Any]:
    """Validate deterministic tier compilation and recovery receipts offline."""

    if iterations < 2:
        raise ValueError("iterations must be at least 2")
    if p95_budget_ms <= 0:
        raise ValueError("p95_budget_ms must be positive")
    items, budget = _fixture()
    durations: list[float] = []
    digests: list[str] = []
    first: dict[str, Any] | None = None
    for _ in range(iterations):
        started = time.perf_counter()
        report = compile_tiered_context(items, budget)
        durations.append((time.perf_counter() - started) * 1_000)
        digests.append(str(report["context_digest"]))
        first = report
    assert first is not None
    reversed_report = compile_tiered_context(tuple(reversed(items)), budget)
    pre_compact = build_context_tier_lifecycle_receipt(
        project="validation-project",
        session_id="validation-session",
        event_id="validation-precompact",
        hook_type="codex_pre_compact",
        source_ids=("synthetic-source-b", "synthetic-source-a", "synthetic-source-a"),
    )
    resume = build_context_tier_lifecycle_receipt(
        project="validation-project",
        session_id="validation-session",
        event_id="validation-resume",
        hook_type="codex_resume",
        parent_event_id="validation-precompact",
    )
    p95 = statistics.quantiles(durations, n=20, method="inclusive")[18]
    included_tiers = [str(item["tier"]) for item in first["included"]]
    omission_reasons = {str(item["reason"]) for item in first["omitted"]}
    execution = first["execution"]
    checks = {
        "deterministic_report": first == reversed_report and len(set(digests)) == 1,
        "all_tiers_in_stable_order": included_tiers == ["working", "session", "project", "archival"],
        "budget_and_duplicate_omissions_explicit": "truncated_to_tier_budget" in {str(item["reason"]) for item in first["included"]} and omission_reasons == {"duplicate_memory_id"},
        "no_storage_or_promotion_mutation": execution == {"sqlite_mutation": False, "qdrant_mutation": False, "promotion": "none"},
        "precompact_anchor_is_content_free": pre_compact["anchor"] is not None and pre_compact["anchor"].get("kind") == "pre_compact_anchor" and "synthetic-source-a" not in json.dumps(pre_compact, sort_keys=True),
        "resume_requires_parent_link": resume["anchor"] is not None and resume["anchor"].get("parent_event_link_present") is True and resume["promotion"].get("action") == "none",
        "p95_within_budget": p95 <= p95_budget_ms,
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "ok": all(checks.values()),
        "checks": checks,
        "fixture": {"synthetic": True, "item_count": len(items), "iterations": iterations},
        "context": {
            "context_digest": first["context_digest"],
            "included_tiers": included_tiers,
            "omission_reasons": sorted(omission_reasons),
            "budgets": first["budgets"],
        },
        "lifecycle": {
            "precompact_phase": pre_compact["phase"],
            "resume_phase": resume["phase"],
            "promotion": "none",
        },
        "latency": {"sample_count": len(durations), "p50_ms": round(statistics.median(durations), 3), "p95_ms": round(p95, 3), "budget_ms": p95_budget_ms},
        "execution": {"network": False, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False, "promotion": "none"},
    }
    report["report_digest"] = hashlib.sha256(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return report


__all__ = ["SCHEMA_VERSION", "build_context_tier_validation_report"]
