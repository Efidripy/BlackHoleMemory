"""Deterministic offline gate for the P17.12 Memory Foundry preview."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from blackholememory.memory_foundry import MEMORY_FOUNDRY_SCHEMA_VERSION
from blackholememory.memory_foundry import build_memory_foundry_preview
from blackholememory.memory_foundry import verify_memory_foundry_digest


def main() -> int:
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    records = [
        {
            "source_id": "validator-m1",
            "project": "blackholememory",
            "memory_type": "feature",
            "content": "Memory Foundry preview",
            "updated_at": "2026-01-01T00:00:00Z",
            "tags": ["bhm", "feature"],
            "metadata": {"raw_title": "Memory Foundry", "confidence": 0.9},
        },
        {
            "source_id": "validator-m2",
            "project": "validator-other",
            "memory_type": "feature",
            "content": "Memory Foundry preview",
            "updated_at": "2026-01-01T00:00:00Z",
            "tags": ["bhm", "feature"],
            "metadata": {"raw_title": "Memory Foundry", "confidence": 0.9},
        },
    ]
    preview = build_memory_foundry_preview(
        records,
        project="blackholememory",
        cross_project_records=records,
        duplicate_candidates=[{"left_id": "validator-m1", "right_id": "validator-m2", "score": 1.0, "reason": "identical_content"}],
        stale_days=30,
        now=now,
    )
    checks = {
        "schema": preview["schema_version"] == MEMORY_FOUNDRY_SCHEMA_VERSION,
        "digest": verify_memory_foundry_digest(preview),
        "fact_crystals": preview["counts"]["fact_crystals"] == 1,
        "duplicate_proposal": any(item["kind"] == "duplicate" for item in preview["proposals"]),
        "stale_review": any(item["kind"] == "stale_review" for item in preview["proposals"]),
        "cross_project_pattern": bool(preview["cross_project_patterns"]),
        "preview_only": preview["mutation"]["writes_performed"] is False and preview["mutation"]["auto_apply"] is False,
        "undo_window": preview["undo"]["available"] is True and preview["undo"]["window_seconds"] == 900,
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
