#!/usr/bin/env python
"""Plan/apply the explicit WL-300.1 additive typed-memory migration.

The command is intentionally operator-gated.  Planning is read-only; applying
requires an exact plan digest, an explicit confirmation, and an offline proof
flag.  It never starts or restarts BHM, Qdrant, Mem0, or the projection worker.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from blackholememory.memory_class_migration import MemoryClassMigrationError  # noqa: E402
from blackholememory.memory_class_migration import apply_migration  # noqa: E402
from blackholememory.memory_class_migration import build_migration_plan  # noqa: E402


DEFAULT_DATABASE = ROOT / ".runtime" / "live-memory" / "memories.sqlite3"
DEFAULT_BACKUP = ROOT / ".runtime" / "backups" / "sqlite-retention"
ARTIFACT_ROOT = ROOT / ".runtime" / "memory-class-migration"


def _artifact_path(path: Path, *, must_exist: bool) -> Path:
    root = ARTIFACT_ROOT.resolve()
    resolved = path.expanduser().resolve(strict=must_exist)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MemoryClassMigrationError(f"artifact must stay below {root}") from exc
    if must_exist and not resolved.is_file():
        raise MemoryClassMigrationError(f"artifact is not a regular file: {resolved}")
    if not must_exist and resolved.exists():
        raise MemoryClassMigrationError(f"artifact already exists: {resolved}")
    return resolved


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--existing-backup",
        type=Path,
        required=True,
        help="verified full SQLite backup from the same authoritative snapshot",
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-digest")
    parser.add_argument("--confirm-operator", action="store_true")
    parser.add_argument("--offline-verified", action="store_true")
    parser.add_argument("--inject-failure", action="store_true")
    args = parser.parse_args(argv)

    try:
        plan_path = _artifact_path(args.plan, must_exist=args.apply)
        receipt_path = _artifact_path(args.receipt, must_exist=False)
        if not args.apply:
            plan = build_migration_plan(args.database, args.existing_backup)
            _write_json(plan_path, plan)
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if not args.expected_plan_digest:
            raise MemoryClassMigrationError("--expected-plan-digest is required with --apply")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise MemoryClassMigrationError("migration plan root must be an object")
        result = apply_migration(
            args.database,
            args.existing_backup,
            plan,
            expected_plan_digest=args.expected_plan_digest,
            confirm_operator=args.confirm_operator,
            offline_verified=args.offline_verified,
            inject_failure=args.inject_failure,
        )
        _write_json(receipt_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (MemoryClassMigrationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
