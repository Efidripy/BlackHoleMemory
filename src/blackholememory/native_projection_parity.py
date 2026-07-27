"""Read-only native ``user_id``/``data`` parity plans for active projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any


REQUIRED_NATIVE_FIELDS = ("user_id", "data")
DEFAULT_PAGE_SIZE = 256
MAX_PAGES = 10_000


def _digest_rows(rows: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows):
        digest.update(row.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _scan_collection(
    client: Any,
    collection_name: str,
    expected_user_id: str,
    *,
    page_size: int,
) -> dict[str, Any]:
    offset: Any = None
    pages = 0
    point_count = 0
    missing_user_scope = 0
    missing_data_field = 0
    mismatched_user_scope = 0
    missing_source_id = 0
    rows: list[str] = []
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
        "missing_required_projection_fields": missing_user_scope + missing_data_field,
        "mismatched_user_scope": mismatched_user_scope,
        "missing_source_id": missing_source_id,
        "pages": pages,
        "rows_digest": _digest_rows(rows),
        "scope": "collection-scoped",
    }


def build_native_projection_parity_plan(
    client: Any,
    collections: Iterable[dict[str, str]],
    *,
    expected_user_id: str,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Build one bounded backfill plan per active collection without writes."""

    if not expected_user_id.strip():
        raise ValueError("expected_user_id must not be empty")
    if not 1 <= page_size <= 1000:
        raise ValueError("page_size must be between 1 and 1000")
    scopes = sorted(
        {
            (str(item.get("project") or "").strip(), str(item.get("collection") or "").strip())
            for item in collections
            if str(item.get("collection") or "").strip()
        },
        key=lambda value: value[1],
    )
    if not scopes:
        raise ValueError("at least one collection is required")
    inventories = [
        {
            "project": project,
            **_scan_collection(client, collection, expected_user_id, page_size=page_size),
        }
        for project, collection in scopes
    ]
    missing = sum(int(item["missing_user_scope"]) for item in inventories)
    missing_data = sum(int(item["missing_data_field"]) for item in inventories)
    mismatched = sum(int(item["mismatched_user_scope"]) for item in inventories)
    missing_source_id = sum(int(item["missing_source_id"]) for item in inventories)
    plan_payload = [
        {
            "project": item["project"],
            "collection": item["collection"],
            "point_count": item["point_count"],
            "missing_user_scope": item["missing_user_scope"],
            "missing_data_field": item["missing_data_field"],
            "mismatched_user_scope": item["mismatched_user_scope"],
            "missing_source_id": item["missing_source_id"],
            "rows_digest": item["rows_digest"],
        }
        for item in inventories
    ]
    plan_digest = hashlib.sha256(
        json.dumps(plan_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "1.0",
        "source": "qdrant-native-projection-parity-read-only",
        "mode": "scoped-backfill-plan",
        "mutation": False,
        "expected_user_id": expected_user_id,
        "required_projection_fields": list(REQUIRED_NATIVE_FIELDS),
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
                f"{item['project']}|{item['collection']}|{item['rows_digest']}" for item in inventories
            ),
            "plan_digest": plan_digest,
        },
        "apply_boundary": {
            "required_before_apply": missing > 0 or missing_data > 0 or mismatched > 0,
            "required_fields": ["project", "collection", "point_id", "source_id", "previous_user_id", "payload_sha256"],
            "verification": "hash-verified backup plus point-count and digest parity",
            "apply_requires": ["explicit apply", "explicit confirmation", "operator-selected backup directory"],
        },
        "ok": mismatched == 0 and missing_source_id == 0,
        "note": "Planning only; no Qdrant payload, SQLite record, archive, or runtime state was changed.",
    }

