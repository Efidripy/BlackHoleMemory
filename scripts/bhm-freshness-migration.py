#!/usr/bin/env python
"""Plan/apply the explicit WL-295.2 SQLite freshness-candidate migration."""

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

from blackholememory.freshness_migration import FreshnessMigrationError  # noqa: E402
from blackholememory.freshness_migration import apply_migration  # noqa: E402
from blackholememory.freshness_migration import build_migration_plan  # noqa: E402


DEFAULT_DATABASE = ROOT / ".runtime" / "live-memory" / "memories.sqlite3"
ARTIFACT_ROOT = ROOT / ".runtime" / "freshness-migration"
SIDECAR_PID = ROOT / ".runtime" / "bootstrap" / "projection-sidecar.pid"


def _under_artifact(path: Path, *, exists: bool) -> Path:
    root = ARTIFACT_ROOT.resolve()
    resolved = path.expanduser().resolve(strict=exists)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FreshnessMigrationError(f"receipt must stay below {root}") from exc
    if exists and not resolved.is_file():
        raise FreshnessMigrationError(f"receipt is not a regular file: {resolved}")
    if not exists and resolved.exists():
        raise FreshnessMigrationError(f"receipt already exists: {resolved}")
    return resolved


def _assert_offline() -> None:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.25):
            raise FreshnessMigrationError("BHM API listener is still active")
    except FreshnessMigrationError:
        raise
    except OSError:
        pass
    if not SIDECAR_PID.is_file():
        return
    try:
        pid = int(SIDECAR_PID.read_text(encoding="utf-8").strip())
        process = psutil.Process(pid)
        command = " ".join(process.cmdline()).casefold()
    except (OSError, ValueError, psutil.NoSuchProcess):
        return
    except psutil.AccessDenied as exc:
        raise FreshnessMigrationError("cannot prove projection sidecar is stopped") from exc
    if "run-bhm-projection-sidecar" in command:
        raise FreshnessMigrationError(f"projection sidecar is still active: {pid}")


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--existing-backup",
        type=Path,
        required=True,
        help="verified full SQLite backup created from the current offline snapshot",
    )
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--plan", type=Path, required=True, help="plan JSON below .runtime/freshness-migration")
    parser.add_argument("--receipt", type=Path, required=True, help="receipt JSON below .runtime/freshness-migration")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-digest")
    parser.add_argument("--confirm-operator", action="store_true")
    parser.add_argument("--inject-failure", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan_path = _under_artifact(args.plan, exists=False if not args.apply else True)
        receipt_path = _under_artifact(args.receipt, exists=False)
        _assert_offline()
        if not args.apply:
            plan = build_migration_plan(args.database, args.existing_backup, as_of=args.as_of)
            _write(plan_path, plan)
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if not args.expected_plan_digest:
            raise FreshnessMigrationError("--expected-plan-digest is required with --apply")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        result = apply_migration(args.database, args.existing_backup, plan, expected_plan_digest=args.expected_plan_digest, confirm_operator=args.confirm_operator, offline_verified=True, inject_failure=args.inject_failure)
        _write(receipt_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (FreshnessMigrationError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
