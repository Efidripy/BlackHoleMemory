from __future__ import annotations

import sqlite3
from pathlib import Path

from blackholememory.freshness_candidates import decide_freshness_candidate
from blackholememory.freshness_candidates import detect_freshness_candidates
from blackholememory.freshness_candidates import list_freshness_candidates
from blackholememory.freshness_candidates import upsert_freshness_candidates
from blackholememory.freshness_migration import SQL_MANIFEST
from blackholememory.memory_repository import SQLiteMemoryRepository


def _fixture(tmp_path: Path) -> Path:
    database = tmp_path / "memories.sqlite3"
    SQLiteMemoryRepository(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.executescript(SQL_MANIFEST)
        connection.execute("PRAGMA user_version=2")
        connection.execute(
            """INSERT INTO memory_revisions(revision_id,memory_id,content,content_sha256,created_at,created_by,metadata_json)
               VALUES ('rev-a','mem-a','old memory','sha-a','2026-01-01T00:00:00Z','fixture','{}')"""
        )
        connection.execute(
            """INSERT INTO memories(memory_id,project,memory_type,lifecycle,title,summary,tags_json,files_json,session_refs_json,upsert_key,created_at,updated_at,provenance_json,metadata_json,extra_json,current_revision_id)
               VALUES ('mem-a','alpha','fact','active','A','A','[]','[]','[]',NULL,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','{}','{}','{}','rev-a')"""
        )
        connection.commit()
    return database


def test_detection_is_deterministic_and_project_scoped(tmp_path: Path) -> None:
    database = _fixture(tmp_path)
    first = detect_freshness_candidates(database, project="alpha", as_of="2026-08-21T00:00:00Z", age_days=30)
    second = detect_freshness_candidates(database, project="alpha", as_of="2026-08-21T00:00:00Z", age_days=30)
    assert first == second
    assert {item["reason_code"] for item in first} == {"age_threshold_reached", "unreferenced"}
    assert detect_freshness_candidates(database, project="beta", as_of="2026-08-21T00:00:00Z") == []
    assert all("content" not in str(item) for item in first)


def test_upsert_is_idempotent_and_decision_is_review_only(tmp_path: Path) -> None:
    database = _fixture(tmp_path)
    candidates = detect_freshness_candidates(database, project="alpha", as_of="2026-08-21T00:00:00Z")
    assert upsert_freshness_candidates(database, candidates) == {"candidates_inserted": 2, "detected_events_appended": 2}
    assert upsert_freshness_candidates(database, candidates) == {"candidates_inserted": 0, "detected_events_appended": 0}
    listed = list_freshness_candidates(database, project="alpha")
    target = listed[0]["candidate_id"]
    result = decide_freshness_candidate(database, project="alpha", candidate_id=target, action="accepted", decision_note="keep after operator review", caller_ref="operator-1", idempotency_key="decision-1")
    assert result["event_appended"] is True
    repeat = decide_freshness_candidate(database, project="alpha", candidate_id=target, action="accepted", decision_note="keep after operator review", caller_ref="operator-1", idempotency_key="decision-1")
    assert repeat["event_appended"] is False
    assert repeat["lifecycle_mutated"] is False
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT lifecycle FROM memories WHERE memory_id='mem-a'").fetchone()[0] == "active"
        assert connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM freshness_candidate_events WHERE action='accepted'").fetchone()[0] == 1


def test_decision_cannot_cross_project_boundary(tmp_path: Path) -> None:
    database = _fixture(tmp_path)
    candidate = detect_freshness_candidates(database, project="alpha", as_of="2026-08-21T00:00:00Z")[0]
    upsert_freshness_candidates(database, [candidate])
    try:
        decide_freshness_candidate(database, project="beta", candidate_id=candidate["candidate_id"], action="dismissed", decision_note="no", caller_ref="operator", idempotency_key="cross-project")
    except Exception as exc:
        assert "requested project" in str(exc)
    else:
        raise AssertionError("cross-project decision unexpectedly succeeded")
