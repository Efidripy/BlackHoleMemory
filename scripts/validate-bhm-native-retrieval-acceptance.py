"""Verify that a native Mem0/Qdrant projection is searchable without fallback."""

from __future__ import annotations

import argparse
import json
from typing import Any

from blackholememory.config import settings
from blackholememory.mem0_adapter import get_project_mem0_memory
from blackholememory.mem0_adapter import get_global_core_memory
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.mem0_adapter import global_collection_name
from blackholememory.mem0_adapter import local_collection_name


def _find_native_point(collection_name: str) -> Any | None:
    client = get_qdrant_client()
    points, _offset = client.scroll(
        collection_name=collection_name,
        limit=1000,
        with_payload=True,
        with_vectors=False,
    )
    for point in points or []:
        payload = dict(getattr(point, "payload", None) or {})
        if payload.get("user_id") == settings.mem0_user_id and str(payload.get("data") or "").strip():
            return point
    return None


def validate(project: str = "blackholememory") -> dict[str, Any]:
    contours = []
    for origin, collection_name, memory in (
        ("LOCAL", local_collection_name(project), get_project_mem0_memory(project)),
        ("GLOBAL", global_collection_name(), get_global_core_memory()),
    ):
        point = _find_native_point(collection_name)
        if point is None:
            contours.append({"origin": origin, "ok": False, "reason": "no native point with user_id and data"})
            continue
        payload = dict(getattr(point, "payload", None) or {})
        content = str(payload.get("data") or "")
        query = " ".join(content.split()[:12])
        result = memory.search(
            query,
            top_k=20,
            filters={
                "user_id": settings.mem0_user_id,
                "AND": [{"source_id": payload.get("source_id")}],
            },
        )
        results = result.get("results") if isinstance(result, dict) else []
        results = results if isinstance(results, list) else []
        matched = [
            item
            for item in results
            if isinstance(item, dict)
            and (
                str((item.get("metadata") or {}).get("source_id") or "") == str(payload.get("source_id") or "")
                or str(item.get("id") or "") == str(getattr(point, "id", "") or "")
            )
        ]
        contours.append(
            {
                "origin": origin,
                "ok": bool(matched),
                "collection": collection_name,
                "point_id": str(getattr(point, "id", "") or ""),
                "source_id": payload.get("source_id"),
                "query": query,
                "result_count": len(results),
                "matched_count": len(matched),
            }
        )
    return {
        "ok": all(bool(item.get("ok")) for item in contours) and len(contours) == 2,
        "mutation": False,
        "project": project,
        "contours": contours,
        "path": "native-mem0-qdrant",
        "note": "Read-only native acceptance; compatibility fallback is not called by this probe.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="blackholememory")
    args = parser.parse_args()
    result = validate(args.project)
    # The Windows console may still be cp1252; escaped JSON keeps the probe
    # machine-readable even when a memory contains Cyrillic or other Unicode.
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
