from __future__ import annotations

from datetime import datetime, timezone

import pytest

from blackholememory.memory_foundry import MemoryFoundryError
from blackholememory.memory_foundry import build_memory_foundry_preview
from blackholememory.memory_foundry import verify_memory_foundry_digest


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _record(memory_id: str, content: str, *, memory_type: str = "knowledge", updated_at: str = "2026-07-01T00:00:00Z") -> dict:
    return {
        "source_id": memory_id,
        "project": "demo",
        "memory_type": memory_type,
        "content": content,
        "updated_at": updated_at,
        "tags": ["bhm", memory_type],
        "metadata": {"raw_title": content[:30], "confidence": 0.9, "files": ["src/demo.py"]},
    }


def test_preview_is_bounded_proposal_only_and_digest_verifiable():
    preview = build_memory_foundry_preview(
        [_record("m1", "Feature alpha", memory_type="feature")],
        project="demo",
        now=NOW,
    )

    assert preview["schema_version"] == "bhm.llm.memory-foundry.v1"
    assert preview["mutation"]["writes_performed"] is False
    assert preview["mutation"]["auto_apply"] is False
    assert preview["undo"]["available"] is True
    assert verify_memory_foundry_digest(preview) is True


def test_fact_and_super_crystals_group_records_without_raw_content_dump():
    preview = build_memory_foundry_preview(
        [_record("m1", "Feature alpha", memory_type="feature"), _record("m2", "Feature beta", memory_type="feature")],
        project="demo",
        now=NOW,
    )

    assert preview["counts"]["fact_crystals"] == 1
    assert preview["fact_crystals"][0]["semantic_type"] == "feature"
    assert preview["fact_crystals"][0]["evidence_count"] == 2
    assert preview["super_crystal"]["fact_crystal_ids"]
    assert "content" not in preview["records"][0]


def test_cross_project_patterns_are_suggestions_with_project_scoped_ids():
    first = _record("m1", "Feature alpha", memory_type="feature")
    second = _record("m2", "Feature beta", memory_type="feature")
    second["project"] = "other-project"
    preview = build_memory_foundry_preview(
        [first],
        project="demo",
        cross_project_records=[first, second],
        now=NOW,
    )

    assert preview["cross_project_patterns"]
    pattern = preview["cross_project_patterns"][0]
    assert set(pattern["projects"]) == {"demo", "other-project"}
    assert pattern["requires_confirmation"] is True
    assert pattern["auto_apply"] is False


def test_detector_candidates_become_confirmation_gated_proposals():
    preview = build_memory_foundry_preview(
        [_record("m1", "same"), _record("m2", "same")],
        project="demo",
        duplicate_candidates=[{"left_id": "m1", "right_id": "m2", "score": 1.0, "reason": "identical_content"}],
        conflict_candidates=[{"left_id": "m1", "right_id": "m2", "score": 0.8, "reason": "same_title_different_content"}],
        relation_candidates=[{"source_id": "m1", "target_id": "m2", "score": 0.6, "reason": "shared_files"}],
        now=NOW,
    )

    kinds = {item["kind"] for item in preview["proposals"]}
    assert {"duplicate", "conflict", "relation"}.issubset(kinds)
    assert all(item["requires_confirmation"] and not item["auto_apply"] for item in preview["proposals"])


def test_stale_review_is_deterministic_and_bounded():
    preview = build_memory_foundry_preview(
        [_record("old", "old memory", updated_at="2025-01-01T00:00:00Z")],
        project="demo",
        stale_days=90,
        now=NOW,
    )

    stale = [item for item in preview["proposals"] if item["kind"] == "stale_review"]
    assert len(stale) == 1
    assert stale[0]["age_days"] > 365


def test_bounds_fail_closed():
    with pytest.raises(MemoryFoundryError):
        build_memory_foundry_preview([], limit=0)
    with pytest.raises(MemoryFoundryError):
        build_memory_foundry_preview([_record(str(index), "x") for index in range(129)])
