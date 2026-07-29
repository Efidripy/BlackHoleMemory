"""Build a deterministic, read-only plan for repairing Qdrant user scopes."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from blackholememory.config import settings
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.mem0_adapter import global_collection_name
from blackholememory.mem0_adapter import local_collection_name


PLAN_VERSION = 1
DEFAULT_PAGE_SIZE = 256
MAX_PAGES = 10000
REQUIRED_PROJECTION_FIELDS = ("user_id", "data")


def _digest_rows(rows: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows):
        digest.update(row.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _collection_inventory(client: Any, collection_name: str, expected_user_id: str, page_size: int) -> dict[str, Any]:
    offset: Any = None
    pages = 0
    point_count = 0
    missing_user_scope = 0
    missing_data_field = 0
    mismatched_user_scope = 0
    missing_source_id = 0
    rows: list[str] = []
    sample_missing: list[str] = []

    while pages < MAX_PAGES:
        points, offset = client.scroll(
            collection_name=collection_name,
            offset=offset,
            limit=page_size,
            with_payload=True,
            with_vectors=False,
        )
        pages += 1
        for point in points or []:
            point_count += 1
            point_id = str(getattr(point, "id", "") or "")
            payload = dict(getattr(point, "payload", None) or {})
            source_id = str(payload.get("source_id") or "")
            user_id = payload.get("user_id")
            if not str(payload.get("data") or "").strip():
                missing_data_field += 1
            if not source_id:
                missing_source_id += 1
            if user_id is None or str(user_id).strip() == "":
                missing_user_scope += 1
                if len(sample_missing) < 10:
                    sample_missing.append(point_id)
            elif str(user_id) != expected_user_id:
                mismatched_user_scope += 1
            rows.append(f"{collection_name}|{point_id}|{source_id}|{user_id or ''}")
        if offset is None:
            break
    else:
        raise RuntimeError(f"scroll page limit exceeded for {collection_name}")

    return {
        "collection": collection_name,
        "point_count": point_count,
        "missing_user_scope": missing_user_scope,
        "missing_data_field": missing_data_field,
        "mismatched_user_scope": mismatched_user_scope,
        "missing_source_id": missing_source_id,
        "sample_missing_point_ids": sample_missing,
        "pages": pages,
        "rows_digest": _digest_rows(rows),
    }


def build_plan(collections: Iterable[str], *, page_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    expected_user_id = str(settings.mem0_user_id)
    normalized_collections = sorted({str(name).strip() for name in collections if str(name).strip()})
    if not normalized_collections:
        raise ValueError("at least one collection is required")
    client = get_qdrant_client()
    inventories = [
        _collection_inventory(client, collection, expected_user_id, page_size)
        for collection in normalized_collections
    ]
    missing = sum(int(item["missing_user_scope"]) for item in inventories)
    missing_data = sum(int(item["missing_data_field"]) for item in inventories)
    mismatched = sum(int(item["mismatched_user_scope"]) for item in inventories)
    missing_source_id = sum(int(item["missing_source_id"]) for item in inventories)
    return {
        "ok": mismatched == 0 and missing_source_id == 0,
        "plan_version": PLAN_VERSION,
        "mode": "read-only-backfill-plan",
        "mutation": False,
        "expected_user_id": expected_user_id,
        "collections": inventories,
        "summary": {
            "collection_count": len(inventories),
            "point_count": sum(int(item["point_count"]) for item in inventories),
            "missing_user_scope": missing,
            "missing_data_field": missing_data,
            "missing_required_projection_fields": missing + missing_data,
            "mismatched_user_scope": mismatched,
            "missing_source_id": missing_source_id,
            "rows_digest": _digest_rows(
                f"{item['collection']}|{item['rows_digest']}" for item in inventories
            ),
        },
        "backup_boundary": {
            "required_before_apply": missing > 0 or missing_data > 0 or mismatched > 0,
            "required_projection_fields": list(REQUIRED_PROJECTION_FIELDS),
            "required_fields": ["collection", "point_id", "source_id", "previous_user_id", "payload_sha256"],
            "verification": "hash-verified backup plus point-count and digest parity",
            "apply_requires": ["explicit --apply", "explicit --confirm", "operator-selected backup directory"],
        },
        "note": "Planning only; no Qdrant payload, SQLite record, archive, or runtime state was changed.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="blackholememory")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.page_size <= 1000:
        parser.error("--page-size must be between 1 and 1000")

    collections = [local_collection_name(args.project), global_collection_name()]
    plan = build_plan(collections, page_size=args.page_size)
    rendered = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if plan["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
