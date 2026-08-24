"""Local operator CLI for explicit governed consolidation proposal workflows.

The command is intentionally inert by default: schema planning and status are
read-only, migration needs an existing backup plus offline proof, and apply
needs both proposal approval and an exact proposal-ID confirmation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from blackholememory.governed_consolidation import GovernedConsolidationRepository
from blackholememory.governed_consolidation import analyze_records
from blackholememory.governed_consolidation import apply_approved_proposal
from blackholememory.governed_consolidation import build_proposal
from blackholememory.governed_consolidation import dry_run_apply
from blackholememory.governed_consolidation import runtime_status
from blackholememory.governed_consolidation import validate_proposal_current
from blackholememory.governed_consolidation_migration import apply_governed_consolidation_migration
from blackholememory.governed_consolidation_migration import build_governed_consolidation_migration_plan
from blackholememory.memory_repository import SQLiteMemoryRepository


def _load_json(path: str) -> Any:
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("status", "plan-migration", "migrate", "create", "list", "inspect", "validate", "approve", "reject", "dry-run", "apply"), default="status")
    parser.add_argument("--database", required=True)
    parser.add_argument("--backup")
    parser.add_argument("--plan-json")
    parser.add_argument("--as-of")
    parser.add_argument("--project")
    parser.add_argument("--proposal-id")
    parser.add_argument("--records-json")
    parser.add_argument("--candidate-json")
    parser.add_argument("--operation", default="create")
    parser.add_argument("--reason")
    parser.add_argument("--confidence", type=float, default=0.75)
    parser.add_argument("--actor")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--offline-writer-verified", action="store_true")
    args = parser.parse_args()
    database = Path(args.database).expanduser()
    try:
        if args.action == "status":
            _emit(runtime_status(database))
            return 0
        if args.action == "plan-migration":
            if not args.backup or not args.as_of:
                raise ValueError("--backup and --as-of are required")
            _emit(build_governed_consolidation_migration_plan(database, args.backup, as_of=args.as_of))
            return 0
        if args.action == "migrate":
            if not args.backup or not args.plan_json:
                raise ValueError("--backup and --plan-json are required")
            plan = _load_json(args.plan_json)
            _emit(apply_governed_consolidation_migration(database, args.backup, plan, expected_plan_digest=str(plan.get("plan_digest") or ""), confirm_operator=args.confirm, offline_verified=args.offline_writer_verified))
            return 0
        if not args.project:
            raise ValueError("--project is required")
        store = GovernedConsolidationRepository(database)
        if args.action == "create":
            if not args.records_json:
                raise ValueError("--records-json is required")
            records = _load_json(args.records_json)
            if not isinstance(records, list):
                raise ValueError("records JSON must be an array")
            if args.candidate_json:
                if not args.reason:
                    raise ValueError("--reason is required with --candidate-json")
                proposal = build_proposal(project=args.project, records=records, operation=args.operation, candidate=_load_json(args.candidate_json), reason=args.reason, confidence=args.confidence)
            else:
                proposal = analyze_records(project=args.project, records=records, operation=args.operation)
            stored, inserted = store.create(proposal)
            _emit({"proposal": stored, "inserted": inserted, "replayed": not inserted})
            return 0
        if args.action == "list":
            _emit({"project": args.project, "proposals": store.list(project=args.project)})
            return 0
        if not args.proposal_id:
            raise ValueError("--proposal-id is required")
        proposal = store.get(args.proposal_id, project=args.project)
        if args.action == "inspect":
            _emit(proposal)
        elif args.action == "validate":
            _emit(validate_proposal_current(proposal=proposal, repository=SQLiteMemoryRepository(database)))
        elif args.action == "dry-run":
            _emit(dry_run_apply(proposal=proposal, repository=SQLiteMemoryRepository(database)))
        elif args.action in {"approve", "reject"}:
            if not args.actor:
                raise ValueError("--actor is required")
            _emit(store.decide(proposal_id=args.proposal_id, project=args.project, decision="approve" if args.action == "approve" else "reject", actor=args.actor))
        else:
            result = apply_approved_proposal(database_path=database, proposal_id=args.proposal_id, project=args.project, apply=args.confirm, confirmation=args.proposal_id)
            _emit({"proposal_id": result.proposal_id, "status": result.status, "memory_ids": list(result.memory_ids), "outbox_event_ids": list(result.outbox_event_ids), "link_id": result.link_id})
        return 0
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        _emit({"ok": False, "error": type(exc).__name__, "detail": str(exc)[:1_000]})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
