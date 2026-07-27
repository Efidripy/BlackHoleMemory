"""Initialize or verify the canonical SQLite runtime store for a BHM install.

The command is deliberately small and idempotent.  It creates only the
SQLite schema required by the authoritative runtime; it never imports
quarantined data, touches Qdrant, or rewrites an existing database. A clean portable
bundle uses this command before starting ``start-bhm-authoritative.ps1``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# ruff: noqa: E402
from blackholememory.memory_repository import SQLiteMemoryRepository
from blackholememory.runtime_storage import inspect_memory_store_schema


def default_database_path(runtime_dir: Path) -> Path:
    """Return the canonical database path below an installation runtime root."""

    return runtime_dir.expanduser().resolve() / "live-memory" / "memories.sqlite3"


def initialize_runtime_database(database_path: Path, *, verify_only: bool = False) -> dict[str, Any]:
    """Create/verify a canonical SQLite target and return a JSON-safe report."""

    path = database_path.expanduser().resolve()
    existed_before = path.exists()
    if verify_only:
        schema_ready, schema_reason = inspect_memory_store_schema(path)
        if not schema_ready:
            return {
                "ok": False,
                "action": "verify",
                "database": str(path),
                "existed_before": existed_before,
                "created": False,
                "schema_ready": False,
                "reason": schema_reason,
            }

    repository = SQLiteMemoryRepository(path)
    if not verify_only:
        repository.initialize()
    health = repository.health()
    schema_ready, schema_reason = inspect_memory_store_schema(path)
    ok = schema_ready and health.schema_version == 1 and health.journal_mode == "wal" and health.quick_check == "ok"
    return {
        "ok": ok,
        "action": "verify" if verify_only else "initialize",
        "database": str(path),
        "existed_before": existed_before,
        "created": not existed_before,
        "schema_ready": schema_ready,
        "schema_reason": schema_reason,
        "schema_version": health.schema_version,
        "journal_mode": health.journal_mode,
        "quick_check": health.quick_check,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=REPO_ROOT / "runtime",
        help="installation runtime root; database is created below live-memory/",
    )
    parser.add_argument("--database", type=Path, help="explicit SQLite database path")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="perform a read-only schema/health check and fail if the target is absent",
    )
    args = parser.parse_args()
    database = args.database or default_database_path(args.runtime_dir)
    result = initialize_runtime_database(database, verify_only=args.verify_only)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
