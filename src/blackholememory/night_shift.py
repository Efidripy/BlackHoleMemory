"""Dry-run-first Night Shift planning for safe local LLM jobs."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


NIGHT_SHIFT_SCHEMA_VERSION = "bhm.llm.night-shift.v1"
NIGHT_SHIFT_MAX_JOBS = 64
NIGHT_SHIFT_SAFE_JOB_TYPES = (
    "memory-summary",
    "retrieval-rewrite",
    "docs-draft",
    "repository-map",
    "test-brainstorm",
    "qa-review",
)
NIGHT_SHIFT_MAX_VRAM_RATIO = 0.85
NIGHT_SHIFT_MAX_TEMPERATURE_C = 82.0


class NightShiftError(ValueError):
    """Raised when Night Shift input exceeds its safety envelope."""


def build_night_shift_preview(
    jobs: Sequence[Mapping[str, Any]],
    *,
    resource_snapshot: Mapping[str, Any] | None = None,
    maintenance_window_open: bool = False,
    user_active: bool = False,
    dry_run: bool = True,
    max_jobs: int = 32,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Plan safe queued work and morning evidence without starting a worker."""

    if len(jobs) > NIGHT_SHIFT_MAX_JOBS:
        raise NightShiftError(f"jobs exceed limit {NIGHT_SHIFT_MAX_JOBS}")
    if not 1 <= int(max_jobs) <= NIGHT_SHIFT_MAX_JOBS:
        raise NightShiftError(f"max_jobs must be between 1 and {NIGHT_SHIFT_MAX_JOBS}")
    snapshot = _normalize_snapshot(resource_snapshot)
    pause_reasons = _pause_reasons(snapshot, maintenance_window_open, user_active)
    normalized = [_normalize_job(job, index) for index, job in enumerate(list(jobs)[:NIGHT_SHIFT_MAX_JOBS])]
    plans: list[dict[str, Any]] = []
    for job in normalized:
        if job["job_type"] not in NIGHT_SHIFT_SAFE_JOB_TYPES:
            plans.append(_job_plan(job, "rejected", ["unsafe_job_type"]))
        elif job["status"] != "queued":
            plans.append(_job_plan(job, "skipped", ["not_queued"]))
        elif job["mutation_requested"]:
            plans.append(_job_plan(job, "rejected", ["mutation_requested"]))
        elif pause_reasons:
            plans.append(_job_plan(job, "paused", pause_reasons))
        elif len([item for item in plans if item["status"] == "admitted"]) >= int(max_jobs):
            plans.append(_job_plan(job, "paused", ["night_shift_capacity"]))
        else:
            plans.append(_job_plan(job, "admitted", ["safe_queued_job", "dry_run_default"]))
    counts = Counter(str(item["status"]) for item in plans)
    proposals = [
        {
            "proposal_id": f"night_{_sha256(str(item['job_id']))[:20]}",
            "job_id": item["job_id"],
            "job_type": item["job_type"],
            "action": "process_if_operator_approved" if item["status"] == "admitted" else "hold",
            "evidence_refs": [item["evidence_ref"]],
            "authority": "proposal",
            "auto_apply": False,
        }
        for item in plans
        if item["status"] in {"admitted", "paused"}
    ][: int(max_jobs)]
    report = {
        "generated_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "resource_snapshot": snapshot,
        "maintenance_window_open": bool(maintenance_window_open),
        "user_active": bool(user_active),
        "pause_reasons": pause_reasons,
        "job_plans": plans,
        "proposals": proposals,
        "counts": dict(sorted(counts.items())),
        "morning_report": {
            "safe_admitted": counts.get("admitted", 0),
            "paused": counts.get("paused", 0),
            "rejected": counts.get("rejected", 0),
            "skipped": counts.get("skipped", 0),
            "summary": "dry-run report; operator review required before any worker start",
            "evidence_refs": [item["evidence_ref"] for item in plans][:32],
        },
    }
    digest = _sha256(_canonical_json(report))
    return {
        "schema_version": NIGHT_SHIFT_SCHEMA_VERSION,
        "preview_digest": digest,
        "dry_run": bool(dry_run),
        **report,
        "execution": {
            "worker_started": False,
            "jobs_claimed": False,
            "writes_performed": False,
            "auto_apply": False,
            "automatic_pause_on_breach": True,
            "authority": "proposal",
        },
        "gates": {
            "safe_job_allowlist": all(item["status"] != "admitted" or item["job_type"] in NIGHT_SHIFT_SAFE_JOB_TYPES for item in plans),
            "mutation_blocked": all(not item["mutation_requested"] for item in normalized),
            "pause_on_resource_breach": True,
            "dry_run_default": bool(dry_run),
            "evidence_present": all(bool(item["evidence_ref"]) for item in plans),
        },
    }


def verify_night_shift_digest(preview: Mapping[str, Any]) -> bool:
    """Verify a Night Shift preview digest."""

    expected = str(preview.get("preview_digest") or "")
    if not expected:
        return False
    core = {
        key: preview.get(key)
        for key in (
            "generated_at",
            "resource_snapshot",
            "maintenance_window_open",
            "user_active",
            "pause_reasons",
            "job_plans",
            "proposals",
            "counts",
            "morning_report",
        )
    }
    return expected == _sha256(_canonical_json(core))


def _normalize_snapshot(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    values = dict(raw or {})
    gpu = bool(values.get("gpu_available", False))
    used = _number(values.get("vram_used_mib"), 0.0)
    total = _number(values.get("vram_total_mib"), 0.0)
    temperature = _number(values.get("temperature_c"), 0.0)
    ratio = round(used / total, 6) if total > 0 else 0.0
    return {
        "gpu_available": gpu,
        "vram_used_mib": round(used, 3),
        "vram_total_mib": round(total, 3),
        "vram_ratio": ratio,
        "temperature_c": round(temperature, 3),
    }


def _pause_reasons(snapshot: Mapping[str, Any], maintenance_window_open: bool, user_active: bool) -> list[str]:
    reasons: list[str] = []
    if user_active:
        reasons.append("interactive_user_activity")
    if not maintenance_window_open:
        reasons.append("maintenance_window_closed")
    if not snapshot["gpu_available"]:
        reasons.append("gpu_unavailable")
    if float(snapshot["vram_ratio"]) > NIGHT_SHIFT_MAX_VRAM_RATIO:
        reasons.append("vram_threshold_breach")
    if float(snapshot["temperature_c"]) > NIGHT_SHIFT_MAX_TEMPERATURE_C:
        reasons.append("temperature_threshold_breach")
    return reasons


def _normalize_job(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    item = dict(raw)
    job_id = str(item.get("job_id") or item.get("id") or f"night-job-{index}").strip()[:160]
    job_type = str(item.get("job_type") or item.get("type") or "").strip()[:96]
    status = str(item.get("status") or "queued").strip().casefold()
    return {
        "job_id": job_id,
        "job_type": job_type,
        "project": str(item.get("project") or "blackholememory")[:120],
        "status": status,
        "priority": int(item.get("priority") or 0),
        "mutation_requested": bool(item.get("mutation_requested") or item.get("mutates")),
        "evidence_ref": f"llm-job:{job_id}",
    }


def _job_plan(job: Mapping[str, Any], status: str, reason_codes: Sequence[str]) -> dict[str, Any]:
    return {
        **dict(job),
        "status": status,
        "reason_codes": list(dict.fromkeys(str(item) for item in reason_codes)),
    }


def _number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if number != number else number


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "NIGHT_SHIFT_SAFE_JOB_TYPES",
    "NIGHT_SHIFT_SCHEMA_VERSION",
    "NightShiftError",
    "build_night_shift_preview",
    "verify_night_shift_digest",
]
