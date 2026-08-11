from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from blackholememory.memory_refinery import apply_normalization_plan
from blackholememory.memory_refinery import build_normalization_plan
from blackholememory.memory_refinery import prepare_rehearsal_copies
from blackholememory.memory_refinery import prove_rollback_restore
from blackholememory.memory_refinery import verify_database
from blackholememory.memory_service import SQLiteMemoryService


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _require_distinct_paths(**paths: Path) -> None:
    resolved: dict[Path, list[str]] = {}
    for label, path in paths.items():
        resolved.setdefault(_resolved(path), []).append(label)
    collisions = [labels for labels in resolved.values() if len(labels) > 1]
    if collisions:
        details = ", ".join("=".join(labels) for labels in collisions)
        raise ValueError(f"refinery artifact paths must be distinct: {details}")


def _require_new_outputs(**paths: Path) -> None:
    existing = [f"{label}={_resolved(path)}" for label, path in paths.items() if path.exists()]
    if existing:
        raise ValueError("refinery output paths must not already exist: " + ", ".join(existing))


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("refinery plan must be a JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Digest-gated BHM memory normalization")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--database", type=Path, required=True)
    plan_parser.add_argument("--output", type=Path, required=True)
    plan_parser.add_argument("--project", default=None)

    rehearse_parser = subparsers.add_parser("rehearse")
    rehearse_parser.add_argument("--database", type=Path, required=True)
    rehearse_parser.add_argument("--backup", type=Path, required=True)
    rehearse_parser.add_argument("--working-copy", type=Path, required=True)
    rehearse_parser.add_argument("--restore-probe", type=Path, required=True)
    rehearse_parser.add_argument("--plan", type=Path, required=True)
    rehearse_parser.add_argument("--receipt", type=Path, required=True)
    rehearse_parser.add_argument("--project", default=None)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--database", type=Path, required=True)
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--expected-plan-digest", required=True)
    apply_parser.add_argument("--allow-live", action="store_true")
    apply_parser.add_argument("--receipt", type=Path, required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--database", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "plan":
        _require_distinct_paths(database=args.database, output=args.output)
        _require_new_outputs(output=args.output)
        records = SQLiteMemoryService(args.database).load_records(
            include_storage_lifecycle=True
        )
        payload = build_normalization_plan(records, project=args.project)
        _write_json(args.output, payload)
        display = {
            "schema_version": payload["schema_version"],
            "output": str(args.output),
            "records_selected": payload["records_selected"],
            "records_changed": payload["records_changed"],
            "plan_digest": payload["plan_digest"],
        }
    elif args.command == "rehearse":
        _require_distinct_paths(
            database=args.database,
            backup=args.backup,
            working_copy=args.working_copy,
            restore_probe=args.restore_probe,
            plan=args.plan,
            receipt=args.receipt,
        )
        _require_new_outputs(
            backup=args.backup,
            working_copy=args.working_copy,
            restore_probe=args.restore_probe,
            plan=args.plan,
            receipt=args.receipt,
        )
        copies = prepare_rehearsal_copies(args.database, args.backup, args.working_copy)
        rollback_backup = copies["rollback_backup"]
        records = SQLiteMemoryService(args.working_copy).load_records(
            include_storage_lifecycle=True
        )
        plan = build_normalization_plan(records, project=args.project)
        _write_json(args.plan, plan)
        applied = apply_normalization_plan(
            args.working_copy,
            plan,
            expected_plan_digest=plan["plan_digest"],
        )
        restore_proof = prove_rollback_restore(
            args.backup,
            args.restore_probe,
            expected_backup_sha256=rollback_backup["sha256_before"],
            expected_fingerprint=rollback_backup["logical_fingerprint"],
        )
        rollback_backup["sha256_after_rehearsal"] = restore_proof["backup_sha256_after"]
        payload = {
            "rollback_backup": rollback_backup,
            "working_copy": copies["working_copy"],
            "restore_proof": restore_proof,
            "plan": str(args.plan),
            "apply": applied,
        }
        _write_json(args.receipt, payload)
        display = {
            "schema_version": applied["schema_version"],
            "rollback_backup": rollback_backup,
            "working_copy": str(args.working_copy),
            "restore_probe": str(args.restore_probe),
            "plan": str(args.plan),
            "receipt": str(args.receipt),
            "records_changed": applied["records_changed"],
            "plan_digest": applied["plan_digest"],
        }
    elif args.command == "apply":
        _require_distinct_paths(database=args.database, plan=args.plan, receipt=args.receipt)
        _require_new_outputs(receipt=args.receipt)
        if not args.allow_live:
            raise ValueError("refinery apply requires explicit --allow-live authorization")
        plan = _read_json(args.plan)
        payload = apply_normalization_plan(
            args.database,
            plan,
            expected_plan_digest=args.expected_plan_digest,
            allow_live=args.allow_live,
        )
        _write_json(args.receipt, payload)
        display = {**payload, "receipt": str(args.receipt)}
    else:
        payload = verify_database(args.database)
        display = payload
    print(json.dumps(display, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
