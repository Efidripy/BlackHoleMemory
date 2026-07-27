from __future__ import annotations

from blackholememory.retrieval_filters import build_candidate_filters


def test_candidate_filters_push_project_and_taxonomy_predicates_downstream():
    filters = build_candidate_filters(
        user_id="user-1",
        project_values={"blackholememory", "BlackHoleMemory"},
        memory_type="knowledge",
        concepts=["qdrant"],
        files=["README.md"],
        domain="infra",
        semantic_type="architecture",
        priority="high",
        include_archived=False,
        include_logs=False,
    )

    assert filters["user_id"] == "user-1"
    assert {"project": {"in": ["BlackHoleMemory", "blackholememory"]}} in filters["AND"]
    assert {"memory_type": "knowledge"} in filters["AND"]
    assert {"tags": ["qdrant"]} in filters["AND"]
    assert {"files": ["README.md"]} in filters["AND"]
    assert {"domain": "infra"} in filters["AND"]
    assert {"semantic_type": "architecture"} in filters["AND"]
    assert {"priority": "high"} in filters["AND"]
    assert {"lifecycle": {"in": ["archived", "deprecated"]}} in filters["NOT"]
    assert {"semantic_type": {"in": ["log", "error"]}} in filters["NOT"]


def test_candidate_filters_keep_user_scope_when_optional_filters_are_empty():
    assert build_candidate_filters(user_id="user-1") == {
        "user_id": "user-1",
        "NOT": [
            {"lifecycle": {"in": ["archived", "deprecated"]}},
            {"semantic_type": {"in": ["log", "error"]}},
        ],
    }
