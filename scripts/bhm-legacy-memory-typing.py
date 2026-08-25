#!/usr/bin/env python
# ruff: noqa: E402
"""Plan/apply conservative deterministic legacy-memory typing locally."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from blackholememory.legacy_memory_typing import LegacyMemoryTypingError
from blackholememory.legacy_memory_typing import apply_legacy_memory_typing
from blackholememory.legacy_memory_typing import build_legacy_memory_typing_plan


DEFAULT_DATABASE = ROOT / ".runtime" / "live-memory" / "memories.sqlite3"
ARTIFACT_ROOT = ROOT / ".runtime" / "legacy-memory-typing"


def _path(path: Path, *, must_exist: bool) -> Path:
    root = ARTIFACT_ROOT.resolve()
    resolved = path.expanduser().resolve(strict=must_exist)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise LegacyMemoryTypingError(f"artifact must stay below {root}") from exc
    if must_exist and not resolved.is_file():
        raise LegacyMemoryTypingError(f"artifact is not a regular file: {resolved}")
    if not must_exist and resolved.exists():
        raise LegacyMemoryTypingError(f"artifact already exists: {resolved}")
    return resolved


def _write(path: Path, payload: dict) -> None:
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
        plan_path = _path(args.plan, must_exist=args.apply)
        receipt_path = _path(args.receipt, must_exist=False)
        if not args.apply:
            plan = build_legacy_memory_typing_plan(args.database, args.existing_backup)
            _write(plan_path, plan)
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if not args.expected_plan_digest:
            raise LegacyMemoryTypingError("--expected-plan-digest is required with --apply")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        receipt = apply_legacy_memory_typing(
            args.database,
            args.existing_backup,
            plan,
            expected_plan_digest=args.expected_plan_digest,
            confirm_operator=args.confirm_operator,
            offline_verified=args.offline_verified,
        )
        _write(receipt_path, receipt)
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (LegacyMemoryTypingError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
