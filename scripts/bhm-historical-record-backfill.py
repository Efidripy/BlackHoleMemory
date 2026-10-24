#!/usr/bin/env python
"""Plan/apply the operator-gated checkpoint/session history classification.

Planning is read-only. Applying requires the exact plan digest, a pre-existing
verified SQLite backup and proof that the authority writer has been stopped.
The command never starts services or writes Qdrant directly.
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

from blackholememory.historical_record_backfill import HistoricalRecordBackfillError  # noqa: E402
from blackholememory.historical_record_backfill import apply_historical_record_backfill  # noqa: E402
from blackholememory.historical_record_backfill import build_historical_record_backfill_plan  # noqa: E402


DEFAULT_DATABASE = ROOT / ".runtime" / "live-memory" / "memories.sqlite3"
ARTIFACT_ROOT = ROOT / ".runtime" / "historical-record-backfill"


def _artifact_path(path: Path, *, must_exist: bool) -> Path:
    root = ARTIFACT_ROOT.resolve()
    resolved = path.expanduser().resolve(strict=must_exist)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HistoricalRecordBackfillError(f"artifact must stay below {root}") from exc
    if must_exist and not resolved.is_file():
        raise HistoricalRecordBackfillError(f"artifact is not a regular file: {resolved}")
    if not must_exist and resolved.exists():
        raise HistoricalRecordBackfillError(f"artifact already exists: {resolved}")
    return resolved


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--existing-backup", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-digest")
    parser.add_argument("--confirm-operator", action="store_true")
    parser.add_argument("--offline-verified", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan_path = _artifact_path(args.plan, must_exist=args.apply)
        receipt_path = _artifact_path(args.receipt, must_exist=False)
        if not args.apply:
            plan = build_historical_record_backfill_plan(args.database, args.existing_backup)
            _write_json(plan_path, plan)
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if not args.expected_plan_digest:
            raise HistoricalRecordBackfillError("--expected-plan-digest is required with --apply")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise HistoricalRecordBackfillError("plan root must be an object")
        receipt = apply_historical_record_backfill(
            args.database,
            args.existing_backup,
            plan,
            expected_plan_digest=args.expected_plan_digest,
            confirm_operator=args.confirm_operator,
            offline_verified=args.offline_verified,
        )
        _write_json(receipt_path, receipt)
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (HistoricalRecordBackfillError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
