"""Deterministic offline gate for the P17.17 Night Shift preview."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from blackholememory.night_shift import NIGHT_SHIFT_SCHEMA_VERSION
from blackholememory.night_shift import build_night_shift_preview
from blackholememory.night_shift import verify_night_shift_digest


def main() -> int:
    preview = build_night_shift_preview(
        [
            {"job_id": "safe-1", "job_type": "memory-summary", "status": "queued"},
            {"job_id": "unsafe-1", "job_type": "release", "status": "queued"},
        ],
        resource_snapshot={"gpu_available": True, "vram_used_mib": 11000, "vram_total_mib": 12000, "temperature_c": 90},
        maintenance_window_open=True,
        user_active=True,
        now=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    checks = {
        "schema": preview["schema_version"] == NIGHT_SHIFT_SCHEMA_VERSION,
        "digest": verify_night_shift_digest(preview),
        "safe_paused": preview["job_plans"][0]["status"] == "paused",
        "unsafe_rejected": preview["job_plans"][1]["status"] == "rejected",
        "user_pause": "interactive_user_activity" in preview["pause_reasons"],
        "vram_pause": "vram_threshold_breach" in preview["pause_reasons"],
        "temperature_pause": "temperature_threshold_breach" in preview["pause_reasons"],
        "morning_report": preview["morning_report"]["paused"] == 1,
        "proposal_only": preview["execution"]["worker_started"] is False and preview["execution"]["writes_performed"] is False,
    }
    report = {
        "ok": all(checks.values()),
        "schema_version": preview["schema_version"],
        "preview_digest": preview["preview_digest"],
        "counts": preview["counts"],
        "checks": checks,
        "execution_enabled": False,
        "auto_apply": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
