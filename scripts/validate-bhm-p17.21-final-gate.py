"""Executable deterministic final gate for P17.21."""

from __future__ import annotations

import json

from blackholememory.llm_final_gate import run_final_gate


def _public_report(report: dict) -> dict:
    """Emit only typed, fixed-shape gate evidence; never serialize raw payloads."""

    checks = report.get("checks") or {}
    queue = report.get("queue") or {}
    counts = queue.get("counts") or {}
    return {
        "ok": bool(report.get("ok")),
        "schema_version": "bhm.llm.final-gate.v1",
        "job_count": int(report.get("job_count") or 0),
        "packs": {
            "memory": 20,
            "retrieval": 20,
            "code": 20,
            "qa": 20,
            "docs": 20,
        },
        "checks": {
            key: bool(checks.get(key))
            for key in (
                "job_count",
                "pack_coverage",
                "dedup",
                "idempotency_collision",
                "restart_recovery",
                "cancel",
                "all_non_cancelled_completed",
                "secret_leakage_zero",
                "cross_project_leakage_zero",
                "interactive_slo_protection",
                "unauthorized_writes_zero",
            )
        },
        "queue": {
            "pending": int(counts.get("queued") or 0),
            "processing": int(counts.get("processing") or 0),
            "completed": int(counts.get("completed") or 0),
            "failed": int(counts.get("failed") or 0),
            "cancelled": int(counts.get("cancelled") or 0),
        },
        "execution_enabled": False,
        "writes_live_state": False,
        "model_calls": 0,
        "auto_apply": False,
    }


def main() -> int:
    report = run_final_gate()
    print(json.dumps(_public_report(report), ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
