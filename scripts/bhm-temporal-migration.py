#!/usr/bin/env python
"""Plan/apply the operator-gated WL-300.2 temporal SQLite migration.

Planning never writes SQLite or starts services. Apply is bound to a reviewed
plan digest, a verified same-snapshot backup, an explicit operator flag, and a
local proof that neither the API writer nor the projection sidecar is active.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

import psutil


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from blackholememory.temporal_migration import TemporalMigrationError  # noqa: E402
from blackholememory.temporal_migration import apply_migration  # noqa: E402
from blackholememory.temporal_migration import build_migration_plan  # noqa: E402


DEFAULT_DATABASE = ROOT / ".runtime" / "live-memory" / "memories.sqlite3"
ARTIFACT_ROOT = ROOT / ".runtime" / "temporal-migration"
SIDECAR_PID = ROOT / ".runtime" / "bootstrap" / "projection-sidecar.pid"


def _artifact_path(path: Path, *, must_exist: bool) -> Path:
    root = ARTIFACT_ROOT.resolve()
    resolved = path.expanduser().resolve(strict=must_exist)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise TemporalMigrationError(f"artifact must stay below {root}") from exc
    if must_exist and not resolved.is_file():
        raise TemporalMigrationError(f"artifact is not a regular file: {resolved}")
    if not must_exist and resolved.exists():
        raise TemporalMigrationError(f"artifact already exists: {resolved}")
    return resolved


def _assert_offline_writer() -> None:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.25):
            raise TemporalMigrationError("BHM API listener is still active")
    except TemporalMigrationError:
        raise
    except OSError:
        pass

    if not SIDECAR_PID.is_file():
        return
    try:
        pid = int(SIDECAR_PID.read_text(encoding="utf-8").strip())
        command = " ".join(psutil.Process(pid).cmdline()).casefold()
    except (OSError, ValueError, psutil.NoSuchProcess):
        return
    except psutil.AccessDenied as exc:
        raise TemporalMigrationError("cannot prove projection sidecar is stopped") from exc
    if "run-bhm-projection-sidecar" in command:
        raise TemporalMigrationError(f"projection sidecar is still active: {pid}")


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
            raise TemporalMigrationError("--expected-plan-digest is required with --apply")
        if not args.confirm_operator:
            raise TemporalMigrationError("--confirm-operator is required with --apply")
        _assert_offline_writer()
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise TemporalMigrationError("migration plan root must be an object")
        result = apply_migration(
            args.database,
            args.existing_backup,
            plan,
            expected_plan_digest=args.expected_plan_digest,
            confirm_operator=True,
            offline_verified=True,
            inject_failure=args.inject_failure,
        )
        _write_json(receipt_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (TemporalMigrationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
