from datetime import datetime, timezone

from blackholememory.session_capture import build_session_capture_preview
from blackholememory.session_capture import verify_session_capture_digest


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _fixture():
    observations = [
        {
            "eventId": "evt-current",
            "sessionId": "sess-1",
            "project": "blackholememory",
            "hookType": "tool.complete",
            "timestamp": "2026-07-16T11:59:00Z",
            "data": {"secret": "should-not-leak", "result": "ok"},
            "recordSha256": "a" * 64,
        },
        {
            "eventId": "evt-current",
            "sessionId": "sess-1",
            "project": "blackholememory",
            "hookType": "tool.complete",
            "timestamp": "2026-07-16T11:59:00Z",
            "data": {"result": "changed"},
            "recordSha256": "b" * 64,
        },
        {
            "eventId": "evt-stale",
            "sessionId": "sess-1",
            "project": "blackholememory",
            "hookType": "tool.start",
            "timestamp": "2026-01-01T00:00:00Z",
            "data": {"transcript": "old"},
            "recordSha256": "c" * 64,
        },
        {
            "eventId": "evt-other-project",
            "sessionId": "sess-1",
            "project": "other",
            "hookType": "tool.complete",
            "timestamp": "2026-07-16T11:00:00Z",
            "data": {"result": "no"},
        },
    ]
    records = [
        {
            "id": "session-record-1",
            "project": "blackholememory",
            "session_id": "sess-1",
            "title": "WI-05",
            "next": "run gate",
            "metadata": {"session_id": "sess-1"},
        }
    ]
    memories = [
        {
            "source_id": "mem-1",
            "project": "blackholememory",
            "memory_type": "decision",
            "content": "SQLite remains authoritative.",
            "tags": ["architecture"],
            "updated_at": "2026-07-16T11:00:00Z",
        },
        {
            "source_id": "mem-2",
            "project": "blackholememory",
            "memory_type": "decision",
            "content": "SQLite remains authoritative.",
            "tags": ["architecture"],
            "updated_at": "2026-01-01T00:00:00Z",
        },
        {
            "source_id": "mem-other",
            "project": "other",
            "memory_type": "decision",
            "content": "must not cross project",
        },
    ]
    return observations, records, memories


def test_session_capture_is_bounded_provenance_safe_and_reversible():
    observations, records, memories = _fixture()
    preview = build_session_capture_preview(
        observations,
        session_records=records,
        memories=memories,
        project="blackholememory",
        session_id="sess-1",
        disclosure="audit",
        token_budget=650,
        max_items=8,
        stale_days=30,
        now=NOW,
    )

    assert verify_session_capture_digest(preview)
    assert preview["execution"]["preview_only"] is True
    assert preview["execution"]["writes_sqlite"] is False
    assert preview["packet"]["diagnostics"]["raw_payload_returned"] is False
    assert preview["packet"]["diagnostics"]["excluded_cross_project_count"] == 2
    assert preview["packet"]["diagnostics"]["duplicate_event_ids"] == ["evt-current"]
    assert preview["packet"]["diagnostics"]["stale_event_count"] == 1
    assert preview["packet"]["diagnostics"]["stale_memory_count"] == 1
    assert preview["packet"]["diagnostics"]["duplicate_memory_groups"] == [["mem-1", "mem-2"]]
    rendered = str(preview)
    assert "should-not-leak" not in rendered
    assert "old" not in rendered
    assert preview["packet"]["forget_preview"]["preview_only"] is True
    assert preview["packet"]["forget_preview"]["undo_token_digest"]
    assert preview["budget"]["estimated_tokens"] <= 650


def test_progressive_disclosure_hides_deep_fields_until_requested():
    observations, records, memories = _fixture()
    brief = build_session_capture_preview(
        observations,
        session_records=records,
        memories=memories,
        project="blackholememory",
        session_id="sess-1",
        disclosure="brief",
        now=NOW,
    )
    standard = build_session_capture_preview(
        observations,
        session_records=records,
        memories=memories,
        project="blackholememory",
        session_id="sess-1",
        disclosure="standard",
        now=NOW,
    )
    assert "fact_crystals" not in brief["packet"]
    assert "content_excerpt" not in brief["packet"]["memories"][0]
    assert "fact_crystals" not in standard["packet"]
    assert "content_excerpt" in standard["packet"]["memories"][0]
