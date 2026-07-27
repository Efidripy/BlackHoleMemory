from __future__ import annotations

from datetime import datetime, timezone

import pytest

from blackholememory.night_shift import NightShiftError
from blackholememory.night_shift import build_night_shift_preview
from blackholememory.night_shift import verify_night_shift_digest


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)
SNAPSHOT = {"gpu_available": True, "vram_used_mib": 4000, "vram_total_mib": 12000, "temperature_c": 55}


def _jobs() -> list[dict]:
    return [
        {"job_id": "safe-1", "job_type": "memory-summary", "status": "queued"},
        {"job_id": "unsafe-1", "job_type": "release", "status": "queued"},
        {"job_id": "mutating-1", "job_type": "docs-draft", "status": "queued", "mutation_requested": True},
    ]


def test_preview_admits_only_safe_jobs_and_is_digest_verifiable():
    preview = build_night_shift_preview(
        _jobs(),
        resource_snapshot=SNAPSHOT,
        maintenance_window_open=True,
        now=NOW,
    )

    statuses = {item["job_id"]: item["status"] for item in preview["job_plans"]}
    assert statuses == {"safe-1": "admitted", "unsafe-1": "rejected", "mutating-1": "rejected"}
    assert preview["gates"]["safe_job_allowlist"] is True
    assert preview["execution"]["worker_started"] is False
    assert preview["execution"]["writes_performed"] is False
    assert verify_night_shift_digest(preview) is True


def test_user_activity_and_resource_breach_pause_jobs():
    preview = build_night_shift_preview(
        [{"job_id": "safe", "job_type": "retrieval-rewrite", "status": "queued"}],
        resource_snapshot={"gpu_available": True, "vram_used_mib": 11000, "vram_total_mib": 12000, "temperature_c": 90},
        maintenance_window_open=True,
        user_active=True,
        now=NOW,
    )

    assert preview["job_plans"][0]["status"] == "paused"
    assert {"interactive_user_activity", "vram_threshold_breach", "temperature_threshold_breach"}.issubset(set(preview["pause_reasons"]))
    assert preview["execution"]["automatic_pause_on_breach"] is True


def test_closed_maintenance_window_pauses_without_masking_reason():
    preview = build_night_shift_preview(
        [{"job_id": "safe", "job_type": "docs-draft", "status": "queued"}],
        resource_snapshot=SNAPSHOT,
        maintenance_window_open=False,
        now=NOW,
    )

    assert preview["job_plans"][0]["status"] == "paused"
    assert "maintenance_window_closed" in preview["job_plans"][0]["reason_codes"]


def test_dry_run_false_still_does_not_start_or_apply():
    preview = build_night_shift_preview(
        [{"job_id": "safe", "job_type": "qa-review", "status": "queued"}],
        resource_snapshot=SNAPSHOT,
        maintenance_window_open=True,
        dry_run=False,
        now=NOW,
    )

    assert preview["dry_run"] is False
    assert preview["execution"]["worker_started"] is False
    assert preview["execution"]["jobs_claimed"] is False
    assert preview["gates"]["dry_run_default"] is False


def test_non_queued_jobs_are_skipped_and_reported():
    preview = build_night_shift_preview(
        [{"job_id": "done", "job_type": "memory-summary", "status": "completed"}],
        resource_snapshot=SNAPSHOT,
        maintenance_window_open=True,
        now=NOW,
    )

    assert preview["job_plans"][0]["status"] == "skipped"
    assert preview["morning_report"]["skipped"] == 1


def test_bounds_fail_closed():
    with pytest.raises(NightShiftError):
        build_night_shift_preview(_jobs(), max_jobs=0)
    with pytest.raises(NightShiftError):
        build_night_shift_preview(_jobs() * 22)
