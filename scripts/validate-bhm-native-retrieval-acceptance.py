"""Verify that a native Mem0/Qdrant projection is searchable without fallback."""

from __future__ import annotations

import argparse
import json
import os
import re
from urllib.error import URLError
from urllib.request import Request
from typing import Any

from blackholememory.config import settings
from blackholememory.local_endpoint_policy import open_local_url
from blackholememory.local_endpoint_policy import read_bounded_response
from blackholememory.mem0_adapter import get_project_mem0_memory
from blackholememory.mem0_adapter import get_global_core_memory
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.mem0_adapter import global_collection_name
from blackholememory.mem0_adapter import local_collection_name
from blackholememory.resource_limits import LLM_INVENTORY_HTTP_TIMEOUT_SECONDS
from blackholememory.runtime_endpoints import endpoint_url


def _provider_is_live(base_url: str) -> bool:
    try:
        request = Request(f"{base_url.rstrip('/')}/models", method="GET")
        with open_local_url(
            request,
            timeout=LLM_INVENTORY_HTTP_TIMEOUT_SECONDS,
            endpoint=base_url,
        ) as response:
            read_bounded_response(response, limit=128)
            return int(getattr(response, "status", 0) or 0) == 200
    except (OSError, URLError, ValueError):
        return False


def _resolve_authoritative_provider_endpoint() -> None:
    """Keep the standalone acceptance probe aligned with the local launcher.

    A historical Docker-host value may remain in ``~/.bhm/.env`` while the
    authoritative Windows runtime and LM Studio are loopback services.  The
    probe is read-only, so it may redirect only this process when that exact
    stale value is present and the canonical loopback provider is live.
    """

    configured = str(settings.mem0_openai_base_url or "").strip().rstrip("/")
    loopback = endpoint_url("lm_studio").rstrip("/")
    if not re.fullmatch(r"https?://172\.18\.0\.1:\d+/v1", configured):
        return
    if not _provider_is_live(loopback):
        return
    settings.mem0_openai_base_url = loopback
    os.environ["OPENAI_BASE_URL"] = loopback
    os.environ["BHM_MEM0_OPENAI_BASE_URL"] = loopback


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
    _resolve_authoritative_provider_endpoint()
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
