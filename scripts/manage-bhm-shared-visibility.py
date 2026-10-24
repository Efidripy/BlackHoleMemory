#!/usr/bin/env python
"""Dry-run-first shared-visibility classification for governed BHM reads.

The command changes only SQLite-authoritative metadata. It never calls Mem0,
Qdrant, an HTTP endpoint, or a lifecycle endpoint. A batch identifier makes
rollback remove only metadata added by that same batch.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from blackholememory.domain import Memory
from blackholememory.memory_service import MemoryServiceNotReady
from blackholememory.memory_service import SQLiteMemoryService
from blackholememory.runtime_storage import resolve_runtime_storage_config


SCHEMA_VERSION = "bhm.governed-shared-memory.visibility-plan.v1"
_BATCH_KEY = "shared_visibility_batch_id"
_CLASSIFIED_AT_KEY = "shared_visibility_classified_at"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _metadata(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("metadata") if isinstance(record.get("metadata"), dict) else {}


def _owner(record: dict[str, Any]) -> str:
    return str(record.get("agent_id") or _metadata(record).get("owner_id") or "").strip()


def _active(record: dict[str, Any]) -> bool:
    metadata = _metadata(record)
    lifecycle = str(record.get("lifecycle") or metadata.get("lifecycle") or "active").strip().casefold()
    return lifecycle not in {"archived", "archive", "deprecated", "tombstone", "tombstoned", "purged", "deleted"} and not bool(
        metadata.get("archived_at") or record.get("archived_at")
    )


def _candidates(
    records: list[dict[str, Any]], *, action: str, project: str, owner_id: str,
    visibility: str, sensitivity: str, batch_id: str,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for record in records:
        metadata = _metadata(record)
        if str(record.get("project") or "") != project or not _active(record) or _owner(record) != owner_id:
            continue
        if action == "classify":
            if str(metadata.get("sensitivity") or "").casefold() != sensitivity or metadata.get("shared_visibility"):
                continue
        elif action == "rollback":
            if metadata.get(_BATCH_KEY) != batch_id or metadata.get("shared_visibility") != visibility:
                continue
        else:
            raise ValueError(f"unsupported action: {action}")
        memory = Memory.from_record(record)
        result.append({
            "memory_id": memory.id,
            "revision_id": memory.current_revision.revision_id,
            "source_digest": str(record.get("source_digest") or metadata.get("source_digest") or ""),
        })
    return sorted(result, key=lambda item: item["memory_id"])


def _plan(
    *, action: str, project: str, owner_id: str, visibility: str, sensitivity: str,
    batch_id: str, candidates: list[dict[str, str]],
) -> dict[str, Any]:
    binding = {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "project": project,
        "owner_id": owner_id,
        "visibility": visibility,
        "sensitivity": sensitivity,
        "batch_id": batch_id,
        "candidates": candidates,
    }
    return {
        **{key: value for key, value in binding.items() if key != "candidates"},
        "candidate_count": len(candidates),
        "candidate_digest": _digest(candidates),
        "plan_digest": _digest(binding),
        "shared_read_enabled": False,
        "shared_write_enabled": False,
        "direct_mem0_mutation": False,
        "direct_qdrant_mutation": False,
        "memory_lifecycle_mutation": False,
    }


def _apply(
    service: SQLiteMemoryService, records: list[dict[str, Any]], *, action: str,
    project: str, owner_id: str, visibility: str, sensitivity: str, batch_id: str,
    expected_plan_digest: str,
) -> dict[str, Any]:
    candidates = _candidates(
        records, action=action, project=project, owner_id=owner_id,
        visibility=visibility, sensitivity=sensitivity, batch_id=batch_id,
    )
    plan = _plan(
        action=action, project=project, owner_id=owner_id, visibility=visibility,
        sensitivity=sensitivity, batch_id=batch_id, candidates=candidates,
    )
    if expected_plan_digest != plan["plan_digest"]:
        raise ValueError("expected plan digest does not match the current authoritative snapshot")
    if not candidates:
        return {**plan, "applied": True, "records_changed": 0}

    by_id = {str(item.get("source_id") or ""): item for item in records}
    expected_revisions = {item["memory_id"]: item["revision_id"] for item in candidates}
    now = _utc_now()
    changed: list[Memory] = []
    for candidate in candidates:
        updated = copy.deepcopy(by_id[candidate["memory_id"]])
        metadata = dict(_metadata(updated))
        if action == "classify":
            metadata["shared_visibility"] = visibility
            metadata[_BATCH_KEY] = batch_id
            metadata[_CLASSIFIED_AT_KEY] = now
        else:
            metadata.pop("shared_visibility", None)
            metadata.pop(_BATCH_KEY, None)
            metadata.pop(_CLASSIFIED_AT_KEY, None)
        updated["metadata"] = metadata
        updated["updated_at"] = now
        changed.append(Memory.from_record(updated))
    service.repository.save_memories_atomic(changed, expected_revision_ids=expected_revisions)
    return {**plan, "applied": True, "records_changed": len(changed)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=Path(".runtime"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--classify", action="store_true")
    group.add_argument("--rollback", action="store_true")
    parser.add_argument("--project", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--visibility", choices=("project", "team", "org_tenant"), default="project")
    parser.add_argument("--sensitivity", choices=("public", "internal", "restricted"), default="internal")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-digest", default="")
    args = parser.parse_args(argv)
    if args.apply and not args.expected_plan_digest:
        parser.error("--apply requires --expected-plan-digest from a dry run")
    try:
        action = "classify" if args.classify else "rollback"
        service = SQLiteMemoryService(resolve_runtime_storage_config(runtime_dir=args.runtime_dir.resolve()).database_path)
        records = service.load_records()
        candidates = _candidates(
            records, action=action, project=args.project, owner_id=args.owner_id,
            visibility=args.visibility, sensitivity=args.sensitivity, batch_id=args.batch_id,
        )
        plan = _plan(
            action=action, project=args.project, owner_id=args.owner_id, visibility=args.visibility,
            sensitivity=args.sensitivity, batch_id=args.batch_id, candidates=candidates,
        )
        if not args.apply:
            print(json.dumps({**plan, "applied": False}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return 0
        print(json.dumps(_apply(
            service, records, action=action, project=args.project, owner_id=args.owner_id,
            visibility=args.visibility, sensitivity=args.sensitivity, batch_id=args.batch_id,
            expected_plan_digest=args.expected_plan_digest,
        ), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (MemoryServiceNotReady, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
