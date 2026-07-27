from __future__ import annotations

from blackholememory.lifecycle_suggestions import build_lifecycle_suggestions
from blackholememory.lifecycle_suggestions import verify_preview_digest


def test_lifecycle_suggestions_are_preview_only_and_cover_actions():
    result = build_lifecycle_suggestions(
        [
            {"queue_id": "dup", "kind": "duplicate", "memory_ids": ["b", "a"], "score": 0.9, "status": "open"},
            {"queue_id": "conf", "kind": "conflict", "memory_ids": ["c", "d"], "score": 0.8, "reasons": ["contradiction"]},
            {"queue_id": "quality", "kind": "quality", "memory_ids": ["e"], "score": 0.7, "reasons": ["low_confidence"]},
        ]
    )

    assert result["mutation"] is False
    assert result["auto_apply"] is False
    assert [item["action"] for item in result["suggestions"]] == [
        "merge_preview",
        "contradiction_review",
        "archive_preview",
    ]
    assert all(item["requires_confirmation"] and item["undo"]["requires_digest"] for item in result["suggestions"])


def test_preview_digest_is_stable_and_detects_plan_drift():
    first = build_lifecycle_suggestions(
        [{"queue_id": "dup", "kind": "duplicate", "memory_ids": ["a", "b"], "score": 0.9}]
    )["suggestions"][0]
    second = build_lifecycle_suggestions(
        [{"queue_id": "dup", "kind": "duplicate", "memory_ids": ["b", "a"], "score": 0.9}]
    )["suggestions"][0]

    assert first["preview_digest"] == second["preview_digest"]
    assert verify_preview_digest(first, first["preview_digest"]) is True
    assert verify_preview_digest({**first, "memory_ids": ["changed"]}, first["preview_digest"]) is False


def test_unknown_or_unactionable_items_are_not_mutation_suggestions():
    result = build_lifecycle_suggestions(
        [{"queue_id": "relation", "kind": "relation_suggestion", "memory_ids": ["a", "b"]}]
    )
    assert result["suggestions"] == []
