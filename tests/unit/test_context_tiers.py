from __future__ import annotations

import hashlib
import json

from blackholememory.context_tier_lifecycle import build_context_tier_lifecycle_receipt
from blackholememory.context_tiers import TierBudget
from blackholememory.context_tiers import TieredContextItem
from blackholememory.context_tiers import compile_tiered_context


def _item(memory_id: str, tier: str, text: str, priority: int = 0) -> TieredContextItem:
    return TieredContextItem(memory_id=memory_id, tier=tier, text=text, priority=priority, source_digest=hashlib.sha256(memory_id.encode()).hexdigest())


def test_tiers_compile_deterministically_in_priority_order() -> None:
    items = (_item("project", "project", "P"), _item("working", "working", "W"), _item("session", "session", "S"))
    first = compile_tiered_context(items, TierBudget(working_bytes=10, session_bytes=10, project_bytes=10, archival_bytes=10))
    assert [row["tier"] for row in first["included"]] == ["working", "session", "project"]
    assert first == compile_tiered_context(tuple(reversed(items)), TierBudget(working_bytes=10, session_bytes=10, project_bytes=10, archival_bytes=10))


def test_tier_budget_and_duplicates_have_explicit_omissions() -> None:
    report = compile_tiered_context((_item("one", "working", "abcdef"), _item("one", "session", "duplicate"), _item("two", "working", "123")), TierBudget(working_bytes=5, session_bytes=10, project_bytes=10, archival_bytes=10))
    assert report["included"][0]["reason"] == "truncated_to_tier_budget"
    assert {item["reason"] for item in report["omitted"]} == {"duplicate_memory_id", "tier_budget_exhausted"}
    assert report["execution"]["promotion"] == "none"


def test_pre_compact_lifecycle_receipt_is_deterministic_and_content_free() -> None:
    first = build_context_tier_lifecycle_receipt(
        project="blackholememory",
        session_id="session-1",
        event_id="event-1",
        hook_type="codex_pre_compact",
        source_ids=("memory-b", "memory-a", "memory-a"),
    )
    second = build_context_tier_lifecycle_receipt(
        project="blackholememory",
        session_id="session-1",
        event_id="event-1",
        hook_type="codex_pre_compact",
        source_ids=("memory-a", "memory-b"),
    )

    assert first == second
    assert first["phase"] == "pre_compact"
    assert first["effect_class"] == "transient_model_context"
    assert first["anchor"]["kind"] == "pre_compact_anchor"
    assert first["promotion"]["action"] == "none"
    assert first["provenance"]["source_count"] == 2
    assert "memory-a" not in json.dumps(first)
    assert "memory-b" not in json.dumps(first)


def test_resume_lifecycle_requires_an_explicit_parent_link() -> None:
    receipt = build_context_tier_lifecycle_receipt(
        project="blackholememory",
        session_id="session-1",
        event_id="event-resume",
        hook_type="codex_resume",
    )

    assert receipt["phase"] == "resume"
    assert receipt["anchor"]["kind"] == "resume_link"
    assert receipt["anchor"]["parent_event_link_present"] is False
    assert receipt["promotion"]["state"] == "policy_gate_disabled"
