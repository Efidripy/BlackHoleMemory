from __future__ import annotations

import copy

from blackholememory import app as bhm_app


def _memory(memory_id: str, content: str) -> dict:
    return {
        "source_id": memory_id,
        "project": "jmaka",
        "memory_type": "knowledge",
        "content": content,
        "tags": ["shared"],
        "metadata": {"raw_title": "same", "files": ["a.cs"], "lifecycle": "active"},
    }


def test_overlap_cleanup_plans_non_overlapping_pairs_and_saves_once(monkeypatch) -> None:
    records = [_memory("source-1", "same"), _memory("target-1", "same")]
    writes: list[list[dict]] = []
    monkeypatch.setattr(bhm_app, "_canonical_project", lambda value: str(value or "").lower())
    monkeypatch.setattr(bhm_app, "_memory_store_is_authoritative", lambda: False)
    monkeypatch.setattr(bhm_app, "_load_live_memories", lambda: copy.deepcopy(records))
    monkeypatch.setattr(bhm_app, "_save_live_memories", lambda items: writes.append(items))
    monkeypatch.setattr(
        bhm_app,
        "_detect_duplicates",
        lambda _request: [{"left_id": "target-1", "right_id": "source-1"}],
    )

    result = bhm_app._overlap_cleanup_apply(
        bhm_app.OverlapCleanupApplyRequest(project="Jmaka", limit=10)
    )

    assert result["count"] == 1
    assert result["committed"] is True
    assert len(writes) == 1
    assert len(writes[0]) == 2
    assert writes[0][0]["metadata"]["merged_from"] == ["source-1"]
    assert writes[0][1]["metadata"]["lifecycle"] == "active"


def test_merge_memory_records_is_pure_and_archives_source() -> None:
    source = _memory("source", "source content")
    target = _memory("target", "target content")

    merged_target, merged_source = bhm_app._merge_memory_records(
        source,
        target,
        archive_source=True,
        target_id="target",
        now="2026-08-10T20:00:00Z",
    )

    assert source["content"] == "source content"
    assert target["content"] == "target content"
    assert "source content" in merged_target["content"]
    assert merged_source["metadata"]["merged_into"] == "target"
    assert merged_source["metadata"]["archived_at"] == "2026-08-10T20:00:00Z"
