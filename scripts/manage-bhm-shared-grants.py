#!/usr/bin/env python
"""Dry-run-first grant/revocation ledger control for WL-300.4.

This command never reads or writes memory content and never enables a shared
memory data route. ``--apply`` appends only immutable SQLite artifacts.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from blackholememory.governed_shared_memory import SharedMemoryGrant
from blackholememory.shared_memory_grants import SharedGrantRevocation
from blackholememory.shared_memory_grants import build_grant_artifact
from blackholememory.shared_memory_grants import build_revocation_artifact
from blackholememory.shared_memory_grants import grant_digest
from blackholememory.runtime_storage import resolve_runtime_storage_config
from blackholememory.memory_service import SQLiteMemoryService


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _service(runtime_dir: Path) -> SQLiteMemoryService:
    return SQLiteMemoryService(resolve_runtime_storage_config(runtime_dir=runtime_dir).database_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=Path(".runtime"))
    parser.add_argument("--grant", action="store_true")
    parser.add_argument("--revoke", action="store_true")
    parser.add_argument("--grant-id", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--owner-id", default="")
    parser.add_argument("--grantee-id", default="")
    parser.add_argument("--visibility", default="project")
    parser.add_argument("--operation", action="append", dest="operations")
    parser.add_argument("--issued-at", default=None)
    parser.add_argument("--expires-at", default=None)
    parser.add_argument("--revoked-at", default=None)
    parser.add_argument("--revocation-receipt-digest", default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.grant == args.revoke:
        parser.error("specify exactly one of --grant or --revoke")

    try:
        runtime_dir = args.runtime_dir.resolve()
        service = _service(runtime_dir)
        if args.grant:
            if not args.owner_id or not args.grantee_id or not args.operations:
                parser.error("--grant requires --owner-id, --grantee-id and --operation")
            grant = SharedMemoryGrant(
                grant_id=args.grant_id,
                project=args.project,
                owner_id=args.owner_id,
                grantee_id=args.grantee_id,
                visibility=args.visibility,
                operations=args.operations,
                issued_at=args.issued_at or _now(),
                expires_at=args.expires_at,
            )
            artifact = build_grant_artifact(grant)
            plan = {
                "schema_version": "bhm.governed-shared-memory.grant-plan.v1",
                "action": "grant",
                "project": grant.project,
                "grant_id": grant.grant_id,
                "grant_digest": grant_digest(grant),
                "artifact": artifact.to_record(),
                "shared_read_enabled": False,
                "shared_write_enabled": False,
            }
        else:
            if not args.revocation_receipt_digest:
                parser.error("--revoke requires --revocation-receipt-digest")
            records = service.list_artifact_records(
                artifact_type="shared_memory_grant",
                project=args.project,
                include_archived=False,
                limit=None,
            )
            grant_record = next(
                (item for item in records if isinstance(item.get("grant"), dict) and item["grant"].get("grant_id") == args.grant_id),
                None,
            )
            if grant_record is None:
                raise ValueError("grant id is not present in the SQLite ledger")
            grant = SharedMemoryGrant.model_validate(grant_record["grant"])
            revocation = SharedGrantRevocation(
                grant_id=grant.grant_id,
                project=grant.project,
                grant_digest=grant_digest(grant),
                revoked_at=args.revoked_at or _now(),
                revocation_receipt_digest=args.revocation_receipt_digest,
            )
            artifact = build_revocation_artifact(revocation)
            plan = {
                "schema_version": "bhm.governed-shared-memory.grant-plan.v1",
                "action": "revoke",
                "project": revocation.project,
                "grant_id": revocation.grant_id,
                "grant_digest": revocation.grant_digest,
                "artifact": artifact.to_record(),
                "shared_read_enabled": False,
                "shared_write_enabled": False,
            }
        if args.apply:
            stored, inserted = service.append_artifact(artifact)
            plan["applied"] = True
            plan["inserted"] = inserted
            plan["stored_artifact_id"] = stored["id"]
        else:
            plan["applied"] = False
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
