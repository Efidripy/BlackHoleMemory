from __future__ import annotations

import sqlite3
from blackholememory.memory_contracts import MemoryMetadata, MetadataScope
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.memory_write_gate import (
    KNOWN_MEMORY_TYPES,
    GateOutcome,
    WriteGateReport,
    attach_gate,
    evaluate_write,
    load_recent_candidates,
    resolve_gate_mode,
    run_write_gate,
)


def test_resolve_gate_mode(monkeypatch) -> None:
    monkeypatch.delenv("BHM_WRITE_GATE", raising=False)
    assert resolve_gate_mode() == "warn"

    monkeypatch.setenv("BHM_WRITE_GATE", "off")
    assert resolve_gate_mode() == "off"

    monkeypatch.setenv("BHM_WRITE_GATE", "enforce")
    assert resolve_gate_mode() == "enforce"

    monkeypatch.setenv("BHM_WRITE_GATE", "unknown-mode")
    assert resolve_gate_mode() == "warn"


def test_suggested_title_word_boundary_latin_and_cyrillic() -> None:
    # Short sentence with period
    report_short = evaluate_write({"content": "First sentence. Second sentence.", "title": None})
    assert report_short["suggested_title"] == "First sentence"

    # Latin sentence longer than 100 characters cut at word boundary
    long_latin = (
        "Deterministic quality check ensures that all new writes follow repository standards "
        "and do not pollute the shared graph index"
    )
    report_latin = evaluate_write({"content": long_latin, "title": None})
    suggested_latin = report_latin["suggested_title"]
    assert suggested_latin is not None
    assert len(suggested_latin) <= 100
    assert not suggested_latin.endswith(" ")
    assert "standards" in suggested_latin
    assert long_latin.startswith(suggested_latin)

    # Cyrillic sentence longer than 100 characters cut at word boundary without splitting words
    long_cyrillic = (
        "Качественная проверка записей в память предотвращает появление мусорных записей "
        "и гарантирует сохранение чистоты графа знаний для всех агентов"
    )
    report_cyrillic = evaluate_write({"content": long_cyrillic, "title": None})
    suggested_cyrillic = report_cyrillic["suggested_title"]
    assert suggested_cyrillic is not None
    assert len(suggested_cyrillic) <= 100
    assert not suggested_cyrillic.endswith(" ")
    assert long_cyrillic.startswith(suggested_cyrillic)

    # When title is provided, suggested_title is None
    report_with_title = evaluate_write(
        {"content": "Some meaningful content here.", "title": "Valid Independent Title"}
    )
    assert report_with_title["suggested_title"] is None


def test_warning_title_missing() -> None:
    report = evaluate_write({"content": "A short note about system behavior.", "title": None})
    codes = [item["code"] for item in report["warnings"]]
    assert "title_missing" in codes
    assert report["suggested_title"] == "A short note about system behavior"


def test_warning_title_not_claim() -> None:
    # Title too short (< 12 chars)
    short_title_report = evaluate_write(
        {"content": "A detailed explanation of memory gates.", "title": "Too short"}
    )
    codes = [item["code"] for item in short_title_report["warnings"]]
    assert "title_not_claim" in codes

    # Title is identical to start of content (snippet pattern)
    snippet_content = "This exact prefix is repeated in the content of the memory record."
    snippet_title = "This exact prefix is repeated"
    snippet_report = evaluate_write(
        {"content": snippet_content, "title": snippet_title}
    )
    codes = [item["code"] for item in snippet_report["warnings"]]
    assert "title_not_claim" in codes

    # Valid independent title (>= 12 chars, not content prefix)
    valid_report = evaluate_write(
        {
            "content": "A detailed explanation of memory gates.",
            "title": "Comprehensive Gate Validation Guide",
            "type": "workflow",
            "metadata": {"scope": "global"},
        }
    )
    valid_codes = [item["code"] for item in valid_report["warnings"]]
    assert "title_not_claim" not in valid_codes
    assert "title_missing" not in valid_codes


def test_warning_content_too_long() -> None:
    long_body = "word " * 310  # 1550 chars
    report_workflow = evaluate_write(
        {"content": long_body, "title": "Valid Independent Title", "type": "workflow"}
    )
    codes_workflow = [item["code"] for item in report_workflow["warnings"]]
    assert "content_too_long" in codes_workflow

    # Playbook type is exempt from content_too_long
    report_playbook = evaluate_write(
        {"content": long_body, "title": "Valid Independent Title", "type": "playbook"}
    )
    codes_playbook = [item["code"] for item in report_playbook["warnings"]]
    assert "content_too_long" not in codes_playbook

    # Content within limit
    short_body = "word " * 100
    report_ok = evaluate_write(
        {"content": short_body, "title": "Valid Independent Title", "type": "fact"}
    )
    codes_ok = [item["code"] for item in report_ok["warnings"]]
    assert "content_too_long" not in codes_ok


def test_warning_type_unknown() -> None:
    for known_type in ("rule", "playbook", "pitfall", "state", "receipt", "workflow", "fact"):
        assert known_type in KNOWN_MEMORY_TYPES
        rep = evaluate_write(
            {"content": "Valid content body", "title": "Valid Independent Title", "type": known_type}
        )
        codes = [item["code"] for item in rep["warnings"]]
        assert "type_unknown" not in codes

    rep_unknown = evaluate_write(
        {"content": "Valid content body", "title": "Valid Independent Title", "type": "unregistered_type"}
    )
    codes_unknown = [item["code"] for item in rep_unknown["warnings"]]
    assert "type_unknown" in codes_unknown


def test_warning_scope_missing() -> None:
    # Missing metadata
    rep_none = evaluate_write(
        {"content": "Valid content body", "title": "Valid Independent Title", "metadata": None}
    )
    assert "scope_missing" in [item["code"] for item in rep_none["warnings"]]

    # Empty metadata dict
    rep_empty = evaluate_write(
        {"content": "Valid content body", "title": "Valid Independent Title", "metadata": {}}
    )
    assert "scope_missing" in [item["code"] for item in rep_empty["warnings"]]

    # Valid scope in dict
    rep_scoped = evaluate_write(
        {
            "content": "Valid content body",
            "title": "Valid Independent Title",
            "metadata": {"scope": "service"},
        }
    )
    assert "scope_missing" not in [item["code"] for item in rep_scoped["warnings"]]

    # Valid scope in MemoryMetadata instance
    rep_model = evaluate_write(
        {
            "content": "Valid content body",
            "title": "Valid Independent Title",
            "metadata": MemoryMetadata(scope=MetadataScope.GLOBAL),
        }
    )
    assert "scope_missing" not in [item["code"] for item in rep_model["warnings"]]


def test_duplicate_detection_by_sha_and_jaccard() -> None:
    existing = [
        {
            "memory_id": "mem_existing_001",
            "project": "proj-a",
            "type": "workflow",
            "content": "Deploy script runs docker compose up with production flag",
            "content_sha256": "digest123",
        },
        {
            "memory_id": "mem_existing_002",
            "project": "proj-a",
            "type": "workflow",
            "content": "Database migrations execute automatically before application startup",
        },
    ]

    # Exact duplicate (sha match)
    rep_sha = evaluate_write(
        {
            "project": "proj-a",
            "type": "workflow",
            "content": "Deploy script runs docker compose up with production flag",
            "content_sha256": "digest123",
        },
        existing=existing,
    )
    assert rep_sha["possible_duplicate_of"] == "mem_existing_001"
    assert rep_sha["possible_duplicate_similarity"] == 1.0
    assert any(item["code"] == "possible_duplicate" for item in rep_sha["warnings"])

    # High Jaccard similarity (>= 0.90) with slight variation
    near_content = "Deploy script runs docker compose up with production flag safely"
    rep_jaccard = evaluate_write(
        {"project": "proj-a", "type": "workflow", "content": near_content},
        existing=existing,
    )
    assert rep_jaccard["possible_duplicate_of"] == "mem_existing_001"
    assert rep_jaccard["possible_duplicate_similarity"] is not None
    assert rep_jaccard["possible_duplicate_similarity"] >= 0.85
    assert any(item["code"] == "possible_duplicate" for item in rep_jaccard["warnings"])

    # Different text: no duplicate false positive
    diff_content = "Frontend builds react components using vite toolchain"
    rep_diff = evaluate_write(
        {"project": "proj-a", "type": "workflow", "content": diff_content},
        existing=existing,
    )
    assert rep_diff["possible_duplicate_of"] is None
    assert not any(item["code"] == "possible_duplicate" for item in rep_diff["warnings"])

    # Candidate from different project or type should not match
    rep_other_proj = evaluate_write(
        {
            "project": "proj-b",
            "type": "workflow",
            "content": "Deploy script runs docker compose up with production flag",
            "content_sha256": "digest123",
        },
        existing=existing,
    )
    assert rep_other_proj["possible_duplicate_of"] is None


def test_load_recent_candidates_sqlite(tmp_path) -> None:
    db_path = tmp_path / "test_memory.sqlite3"
    repo = SQLiteMemoryRepository(db_path)
    repo.initialize()

    # Insert test rows directly matching the repo schema
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO memory_revisions (revision_id, memory_id, content, content_sha256, created_at, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("rev_1", "mem_1", "Active content text", "sha_1", "2026-10-29T00:00:00Z", "{}"),
        )
        conn.execute(
            "INSERT INTO memories (memory_id, project, memory_type, lifecycle, tags_json, files_json, session_refs_json, created_at, updated_at, provenance_json, metadata_json, extra_json, current_revision_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("mem_1", "test-proj", "workflow", "active", "[]", "[]", "[]", "2026-10-29T00:00:00Z", "2026-10-29T00:00:00Z", "{}", "{\"scope\": \"local\"}", "{}", "rev_1"),
        )
        # Archived memory should be ignored
        conn.execute(
            "INSERT INTO memory_revisions (revision_id, memory_id, content, content_sha256, created_at, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("rev_2", "mem_2", "Archived content text", "sha_2", "2026-10-29T00:00:00Z", "{}"),
        )
        conn.execute(
            "INSERT INTO memories (memory_id, project, memory_type, lifecycle, tags_json, files_json, session_refs_json, created_at, updated_at, provenance_json, metadata_json, extra_json, current_revision_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("mem_2", "test-proj", "workflow", "archived", "[]", "[]", "[]", "2026-10-29T00:00:00Z", "2026-10-29T00:00:00Z", "{}", "{}", "{}", "rev_2"),
        )
        conn.commit()

    candidates = load_recent_candidates(db_path, project="test-proj", memory_type="workflow")
    assert len(candidates) == 1
    assert candidates[0]["memory_id"] == "mem_1"
    assert candidates[0]["content"] == "Active content text"
    assert candidates[0]["content_sha256"] == "sha_1"
    assert candidates[0]["metadata"]["scope"] == "local"

    # Querying a non-existent database path returns empty list safely
    assert load_recent_candidates(tmp_path / "non_existent.sqlite3", project="p", memory_type="t") == []


def test_gate_outcome_and_attach_gate() -> None:
    # Off mode
    outcome_off = GateOutcome(mode="off", result=None, reject_detail=None)
    resp_off = {"success": True}
    assert attach_gate(resp_off, outcome_off) == {"success": True}

    # Warn mode attaches gate payload
    outcome_warn = GateOutcome(
        mode="warn",
        result={
            "warnings": [{"code": "title_missing", "detail": "title is missing"}],
            "suggested_title": "Suggested Title",
            "possible_duplicate_of": None,
            "possible_duplicate_similarity": None,
        },
        reject_detail=None,
    )
    resp_warn = {"success": True}
    attached = attach_gate(resp_warn, outcome_warn)
    assert "gate" in attached
    assert attached["gate"]["mode"] == "warn"
    assert attached["gate"]["suggested_title"] == "Suggested Title"
    assert len(attached["gate"]["warnings"]) == 1


def test_run_write_gate_enforce_and_warn() -> None:
    req_empty = {"content": "", "project": "p", "type": "workflow"}
    # Enforce rejects empty content
    outcome_enforce = run_write_gate(req_empty, mode="enforce")
    assert outcome_enforce.reject_detail is not None
    assert outcome_enforce.reject_detail["code"] == "memory_write_gate_rejected"

    # Warn accepts empty content with warning
    outcome_warn = run_write_gate(req_empty, mode="warn")
    assert outcome_warn.reject_detail is None
    assert any(item["code"] == "content_empty" for item in outcome_warn.result["warnings"])

    # Off bypasses evaluation
    outcome_off = run_write_gate(req_empty, mode="off")
    assert outcome_off.result is None
    assert outcome_off.reject_detail is None


def test_write_gate_report_dataclass() -> None:
    report = WriteGateReport(
        warnings=({"code": "title_missing", "detail": "missing"},),
        suggested_title="Title",
        possible_duplicate_of="mem_1",
        possible_duplicate_similarity=0.95,
    )
    as_dict = report.as_dict()
    assert as_dict["suggested_title"] == "Title"
    assert as_dict["possible_duplicate_of"] == "mem_1"
    assert as_dict["possible_duplicate_similarity"] == 0.95
    assert len(as_dict["warnings"]) == 1
