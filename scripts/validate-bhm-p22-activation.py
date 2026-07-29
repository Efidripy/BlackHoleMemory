#!/usr/bin/env python3
"""Create and verify the reversible P22 CBM activation passport.

This is intentionally a bounded operator tool.  It never flips CBM flags and
never writes Qdrant or memory rows; it only snapshots the activation baseline
and creates an online SQLite backup for the next gated workstream.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "cbm-integration.json"
DATABASE = ROOT / "runtime" / "live-memory" / "memories.sqlite3"
BACKUP_ROOT = ROOT / "runtime" / "live-memory" / "recovery-backups"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def qdrant_collections(base_url: str) -> dict[str, Any]:
    request = urllib.request.Request(base_url.rstrip("/") + "/collections", method="GET")
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result = payload.get("result") if isinstance(payload, dict) else None
    collections = result.get("collections") if isinstance(result, dict) else []
    return {"ok": isinstance(collections, list), "collections": collections if isinstance(collections, list) else []}


def online_backup(source: Path, target: Path) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise RuntimeError(f"backup already exists: {target}")
    source_db = sqlite3.connect(str(source))
    target_db = sqlite3.connect(str(target))
    try:
        source_db.backup(target_db)
    finally:
        target_db.close()
        source_db.close()
    with sqlite3.connect(str(target)) as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    return {"path": str(target), "sha256": sha256_file(target), "quick_check": quick_check}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    flags = config.get("feature_flags") or {}
    enabled = sorted(name for name, value in flags.items() if value is True)
    if enabled:
        raise RuntimeError(f"P22.0 requires all CBM flags off; enabled={enabled}")
    if not DATABASE.exists():
        raise RuntimeError(f"authoritative database missing: {DATABASE}")
    backup = args.backup or BACKUP_ROOT / "memories-before-p22-cbm-live-activation-20260721.sqlite3"
    backup_info = online_backup(DATABASE, backup)
    payload = {
        "schema_version": "bhm.p22.activation-passport.v1",
        "ok": backup_info["quick_check"] == "ok",
        "plan_id": "BHM-V6-CBM-LIVE-20260721",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feature_config": str(CONFIG),
        "feature_config_sha256": sha256_file(CONFIG),
        "enabled_flags": enabled,
        "all_flags_off": not enabled,
        "authoritative_database": str(DATABASE),
        "database_sha256_before": sha256_file(DATABASE),
        "sqlite_backup": backup_info,
        "wal_present": (DATABASE.with_name(DATABASE.name + "-wal")).exists(),
        "shm_present": (DATABASE.with_name(DATABASE.name + "-shm")).exists(),
        "qdrant": qdrant_collections(args.qdrant_url),
        "runtime": {
            "ready_url": args.base_url.rstrip("/") + "/health/ready",
            "slo_url": args.base_url.rstrip("/") + "/bhm/health/slo",
            "writes_authority": False,
            "writes_qdrant": False,
        },
        "rollback": {
            "disable_all_flags": True,
            "restore_sqlite_backup": str(backup),
            "reconcile_qdrant_before_rebuild": True,
            "source_quarantine_untouched": True,
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
