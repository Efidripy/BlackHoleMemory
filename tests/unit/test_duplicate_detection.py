from __future__ import annotations

from blackholememory import app as bhm_app


def _record(index: int, *, title: str, content: str, files: list[str] | None = None) -> dict:
    return {
        "source_id": f"memory-{index:04d}",
        "project": "jmaka",
        "memory_type": "knowledge",
        "content": content,
        "tags": [],
        "metadata": {
            "raw_title": title,
            "files": files or [],
            "lifecycle": "active",
        },
    }


def test_duplicate_detection_preserves_reason_precedence(monkeypatch) -> None:
    records = [
        _record(1, title="Same", content="identical", files=["a.cs"]),
        _record(2, title="Same", content="identical", files=["a.cs"]),
        _record(3, title="Same", content="different", files=["a.cs"]),
    ]
    monkeypatch.setattr(bhm_app, "_load_live_memories", lambda: records)

    result = bhm_app._detect_duplicates(
        bhm_app.MemoryDetectRequest(project="jmaka", limit=10, include_archived=False)
    )

    assert result[0]["reason"] == "identical_content"
    assert {(item["left_id"], item["right_id"], item["reason"]) for item in result} == {
        ("memory-0001", "memory-0002", "identical_content"),
        ("memory-0001", "memory-0003", "same_title_same_files"),
        ("memory-0002", "memory-0003", "same_title_same_files"),
    }


def test_duplicate_detection_bounds_large_candidate_bucket(monkeypatch) -> None:
    records = [
        _record(index, title="Shared title", content=f"content {index}")
        for index in range(1_000)
    ]
    normalized_calls = 0
    original = bhm_app._normalized_text

    def counted(value):
        nonlocal normalized_calls
        normalized_calls += 1
        return original(value)

    monkeypatch.setattr(bhm_app, "_load_live_memories", lambda: records)
    monkeypatch.setattr(bhm_app, "_normalized_text", counted)

    result = bhm_app._detect_duplicates(
        bhm_app.MemoryDetectRequest(project="jmaka", limit=10, include_archived=False)
    )

    assert len(result) == 10
    assert normalized_calls == len(records)
    assert all(item["reason"] == "same_title" for item in result)
