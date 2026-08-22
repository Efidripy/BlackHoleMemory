from __future__ import annotations

import hashlib

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
