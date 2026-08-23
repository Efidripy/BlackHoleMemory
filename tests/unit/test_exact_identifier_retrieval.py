from __future__ import annotations

import pytest

from blackholememory.exact_identifier_retrieval import ExactIdentifierIndex
from blackholememory.exact_identifier_retrieval import build_exact_identifier_hits
from blackholememory.exact_identifier_retrieval import exact_identifier_tokens


def _record(
    source_id: str,
    *,
    project: str = "blackholememory",
    content: str = "contract_001_anchor is validated",
    lifecycle: str = "active",
    semantic_type: str = "architecture",
) -> dict:
    return {
        "source_id": source_id,
        "project": project,
        "content": content,
        "memory_type": "semantic",
        "lifecycle": lifecycle,
        "updated_at": "2026-08-23T00:00:00Z",
        "metadata": {
            "content_sha256": "a" * 64,
            "lifecycle": lifecycle,
            "semantic_type": semantic_type,
        },
    }


def test_exact_identifier_tokens_reject_prose_and_require_high_signal_shape():
    assert exact_identifier_tokens("find contract_001_anchor now") == ("contract_001_anchor",)
    assert exact_identifier_tokens("ordinaryword no_digits_here") == ()
    assert exact_identifier_tokens("contract_001_anchor", query=True) == ("contract_001_anchor",)


def test_index_is_project_scoped_and_deterministic():
    records = [
        _record("a", content="contract_001_anchor"),
        _record("b", content="contract_001_anchor", project="other-project"),
    ]
    first = ExactIdentifierIndex.build(records)
    second = ExactIdentifierIndex.build(list(reversed(records)))
    assert first.lookup("find contract_001_anchor", project="blackholememory") == ["a"]
    assert first.lookup("find contract_001_anchor", project="other-project") == ["b"]
    assert first.snapshot_digest == second.snapshot_digest
    assert first.schema_version == "bhm.exact-identifier-retrieval.v1"


def test_index_excludes_archived_tombstoned_and_log_records():
    records = [
        _record("active", content="contract_002_anchor"),
        _record("archived", content="contract_002_anchor", lifecycle="archived"),
        _record("log", content="contract_002_anchor", semantic_type="log"),
        _record("tombstone", content="contract_002_anchor", lifecycle="tombstoned"),
    ]
    index = ExactIdentifierIndex.build(records)
    assert index.lookup("contract_002_anchor", project="blackholememory") == ["active"]


def test_include_record_applies_authoritative_filters_before_indexing():
    records = [
        _record("keep", content="contract_003_anchor"),
        _record("drop", content="contract_003_anchor"),
    ]
    index = ExactIdentifierIndex.build(records, include_record=lambda record: record["source_id"] == "keep")
    assert index.lookup("contract_003_anchor", project="blackholememory") == ["keep"]


def test_lookup_is_bounded_and_hydration_preserves_authoritative_content():
    records = [_record(f"id-{index:03d}", content="contract_004_anchor") for index in range(5)]
    index = ExactIdentifierIndex.build(records)
    source_ids = index.lookup("contract_004_anchor", project="blackholememory", limit=2)
    hits = build_exact_identifier_hits(records, source_ids)
    assert source_ids == ["id-000", "id-001"]
    assert [hit["id"] for hit in hits] == source_ids
    assert all(hit["metadata"]["retrieval_route"] == "exact-identifier" for hit in hits)
    assert all(hit["memory"] == "contract_004_anchor" for hit in hits)


def test_build_rejects_unbounded_snapshot():
    records = (_record(f"id-{index:05d}", content="contract_005_anchor") for index in range(50_001))
    with pytest.raises(ValueError, match="exceeds"):
        ExactIdentifierIndex.build(records)
