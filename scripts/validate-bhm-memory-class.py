#!/usr/bin/env python
"""Run WL-300.1 memory-class checks against disposable SQLite fixtures only."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from blackholememory.freshness_migration import apply_migration as apply_freshness  # noqa: E402
from blackholememory.freshness_migration import build_migration_plan as build_freshness_plan  # noqa: E402
from blackholememory.memory_class_migration import apply_migration  # noqa: E402
from blackholememory.memory_class_migration import build_migration_plan  # noqa: E402
from blackholememory.memory_repository import SQLiteMemoryRepository  # noqa: E402


def _fixture(root: Path) -> tuple[Path, Path]:
    database = root / "memory.sqlite3"
    repository = SQLiteMemoryRepository(database)
    repository.initialize()
    v1_backup = root / "memory-v1.sqlite3"
    shutil.copy2(database, v1_backup)
    freshness_plan = build_freshness_plan(database, v1_backup, as_of="2026-08-22T00:00:00Z")
    apply_freshness(
        database,
        v1_backup,
        freshness_plan,
        expected_plan_digest=freshness_plan["plan_digest"],
        confirm_operator=True,
        offline_verified=True,
    )
    backup = root / "memory-v2.sqlite3"
    shutil.copy2(database, backup)
    return database, backup


def _check() -> dict:
    raw_root = tempfile.mkdtemp(prefix="bhm-wl300-1-")
    try:
        root = Path(raw_root)
        database, backup = _fixture(root)
        plan = build_migration_plan(database, backup)
        before = database.read_bytes()
        result = apply_migration(
            database,
            backup,
            plan,
            expected_plan_digest=plan["plan_digest"],
            confirm_operator=True,
            offline_verified=True,
        )
        with sqlite3.connect(database) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memories)")}
            marker = connection.execute(
                "SELECT value FROM memory_store_meta WHERE key = 'typed_memory_contract_version'"
            ).fetchone()
        return {
            "plan_read_only": database.read_bytes() == before,
            "apply_ok": bool(result.get("ok")),
            "user_version": version,
            "typed_columns_present": {"memory_class", "event_role"}.issubset(columns),
            "capability_marker": str(marker[0]) if marker else None,
            "post_commit_confirmation": result.get("post_commit_confirmation"),
        }
    finally:
        # Windows can retain a transient SQLite sidecar handle briefly after a
        # read-only fingerprint.  Fixture cleanup is best-effort and never
        # touches the live runtime tree.
        shutil.rmtree(raw_root, ignore_errors=True)


def main() -> int:
    checks = _check()
    payload = {
        "schema_version": "bhm.wl300.1.memory-class-validation.v1",
        "writes_live_state": False,
        "sqlite_fixture_only": True,
        "checks": checks,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if all(
        (
            checks["plan_read_only"],
            checks["apply_ok"],
            checks["user_version"] == 2,
            checks["typed_columns_present"],
            checks["capability_marker"] == "1",
            checks["post_commit_confirmation"]["ok"],
        )
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
