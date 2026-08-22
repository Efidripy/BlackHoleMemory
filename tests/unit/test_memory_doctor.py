from __future__ import annotations

from blackholememory.memory_doctor import run_memory_doctor


def test_doctor_is_read_only_redacted_and_deterministic() -> None:
    records = (
        {"source_id": "m1", "project": "p", "content": "secret one", "authority_seq": 3, "projection_seq": 2},
        {"source_id": "m2", "project": "p", "content": "secret one"},
        {"source_id": "m3", "project": "p", "content": "different", "supersedes_revision_id": "rev1"},
    )
    report = run_memory_doctor(records, projection_watermark=2)
    assert report == run_memory_doctor(tuple(reversed(records)), projection_watermark=2)
    codes = {item["reason_code"] for item in report["findings"]}
    assert {"exact_active_duplicate", "projection_stale", "projection_watermark_lag", "supersession_lineage_incomplete"} <= codes
    assert "secret one" not in str(report)
    assert report["execution"]["repair_apply"] is False


def test_doctor_exposes_invalid_identity_without_failing_open() -> None:
    report = run_memory_doctor(({"content": "x"},))
    assert report["findings"][0]["reason_code"] == "memory_identity_missing"
