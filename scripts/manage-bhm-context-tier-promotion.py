#!/usr/bin/env python
"""Operate the default-deny BHM context-tier promotion capability locally.

This operator utility never starts BHM or a projector.  `plan` and `dry-run`
are read-only.  `apply` / `rollback` require the explicit environment policy,
the exact candidate id and a separately completed additive schema migration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blackholememory.context_tier_promotion import apply_tier_promotion
from blackholememory.context_tier_promotion import build_tier_promotion_plan
from blackholememory.context_tier_promotion import dry_run_tier_promotion
from blackholememory.context_tier_promotion import rollback_tier_promotion
from blackholememory.memory_repository import SQLiteMemoryRepository


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("plan must be a JSON object")
    return value


def _write_json(path: Path | None, value: object) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path is None:
        print(rendered, end="")
    else:
        path.expanduser().resolve().write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--database", type=Path, required=True)
    plan.add_argument("--project", required=True)
    plan.add_argument("--session-id", required=True)
    plan.add_argument("--source-memory-id", required=True)
    plan.add_argument("--candidate-content", required=True)
    plan.add_argument("--title", required=True)
    plan.add_argument("--memory-type", default="fact")
    plan.add_argument("--output", type=Path)
    dry_run = commands.add_parser("dry-run")
    apply = commands.add_parser("apply")
    rollback = commands.add_parser("rollback")
    for command in (dry_run, apply):
        command.add_argument("--database", type=Path, required=True)
        command.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--confirmation", required=True)
    rollback.add_argument("--database", type=Path, required=True)
    rollback.add_argument("--candidate-id", required=True)
    rollback.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = build_tier_promotion_plan(
                repository=SQLiteMemoryRepository(args.database), project=args.project,
                session_id=args.session_id, source_memory_ids=[args.source_memory_id],
                candidate_content=args.candidate_content, title=args.title, memory_type=args.memory_type,
            )
            _write_json(args.output, result)
            return 0
        if args.command == "dry-run":
            result = dry_run_tier_promotion(repository=SQLiteMemoryRepository(args.database), plan=_read_json(args.plan))
            _write_json(None, result)
            return 0 if result["current"] else 2
        if args.command == "apply":
            result = apply_tier_promotion(database_path=args.database, plan=_read_json(args.plan), apply=True, confirmation=args.confirmation)
        else:
            result = rollback_tier_promotion(database_path=args.database, candidate_id=args.candidate_id, apply=True, confirmation=args.confirmation)
        _write_json(None, result.__dict__)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
