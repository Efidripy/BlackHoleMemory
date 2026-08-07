#!/usr/bin/env python3
# ruff: noqa: E402
"""Safely remove temporary BHM smoke collections from Qdrant."""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.runtime_endpoints import endpoint_url
from blackholememory.local_endpoint_policy import open_local_url
from blackholememory.local_endpoint_policy import read_bounded_response
from blackholememory.resource_limits import QDRANT_OPERATOR_HTTP_TIMEOUT_SECONDS


DEFAULT_BASE_URL = endpoint_url("qdrant_http")
# Compatibility name retained; the registry-backed operator bound is canonical.
DEFAULT_TIMEOUT_SECONDS = float(QDRANT_OPERATOR_HTTP_TIMEOUT_SECONDS)

PROTECTED_COLLECTIONS = {
    "bhm_global_core_knowledge",
    "bhm_local_memory_agent_memory_codex_connector",
    "bhm_local_memory_agentmemory_private",
    "bhm_local_memory_blackholememory",
    "bhm_local_memory_e_github_workspace",
    "bhm_local_memory_lnv_push",
    "bhm_local_memory_multiserversubgen",
    "bhm_local_memory_sojmieblo",
    "blackholememory-mem0",
    "mem0migrations",
}

CLEANUP_MARKERS = ("smoke", "debug", "demo", "quarantine")


@dataclass(frozen=True)
class CollectionInfo:
    name: str
    points_count: int | None
    status: str


def bounded_qdrant_operator_timeout(value: float) -> float:
    """Clamp Qdrant operator HTTP waits to the finite registry-backed bound."""

    try:
        requested = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Qdrant operator timeout must be numeric") from exc
    if not math.isfinite(requested):
        raise ValueError("Qdrant operator timeout must be finite")
    return max(min(requested, float(QDRANT_OPERATOR_HTTP_TIMEOUT_SECONDS)), 1.0)


def request_json(base_url: str, method: str, path: str, *, timeout: float) -> dict[str, Any]:
    bounded_timeout = bounded_qdrant_operator_timeout(timeout)
    request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", method=method)
    with open_local_url(request, timeout=bounded_timeout) as response:
        raw = read_bounded_response(response).decode("utf-8")
    if not raw:
        return {}
    return json.loads(raw)


def collection_names(base_url: str, *, timeout: float) -> list[str]:
    data = request_json(base_url, "GET", "/collections", timeout=timeout)
    return sorted(str(item["name"]) for item in data["result"]["collections"])


def collection_info(base_url: str, name: str, *, timeout: float) -> CollectionInfo:
    encoded = urllib.parse.quote(name, safe="")
    data = request_json(base_url, "GET", f"/collections/{encoded}", timeout=timeout)
    result = data.get("result") or {}
    points_count = result.get("points_count")
    return CollectionInfo(
        name=name,
        points_count=int(points_count) if points_count is not None else None,
        status=str(result.get("status") or "unknown"),
    )


def is_cleanup_candidate(name: str) -> bool:
    lowered = name.lower()
    if name in PROTECTED_COLLECTIONS:
        return False
    if not name.startswith("bhm_local_memory_"):
        return False
    return any(marker in lowered for marker in CLEANUP_MARKERS)


def delete_collection(base_url: str, name: str, *, timeout: float) -> None:
    encoded = urllib.parse.quote(name, safe="")
    request_json(base_url, "DELETE", f"/collections/{encoded}", timeout=timeout)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Qdrant HTTP base URL.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="HTTP timeout in seconds.")
    parser.add_argument("--apply", action="store_true", help="Delete candidate collections. Default is dry-run.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the final report as JSON instead of human-readable text.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.timeout = bounded_qdrant_operator_timeout(args.timeout)
    names = collection_names(args.base_url, timeout=args.timeout)
    candidates = [name for name in names if is_cleanup_candidate(name)]
    protected_present = [name for name in names if name in PROTECTED_COLLECTIONS]

    infos: list[CollectionInfo] = []
    for name in candidates:
        try:
            infos.append(collection_info(args.base_url, name, timeout=args.timeout))
        except Exception:
            infos.append(CollectionInfo(name=name, points_count=None, status="info_failed"))

    failures: list[dict[str, str]] = []
    deleted: list[str] = []
    if args.apply:
        for info in infos:
            try:
                delete_collection(args.base_url, info.name, timeout=args.timeout)
            except Exception as exc:  # keep deleting the rest, then fail loudly.
                failures.append({"name": info.name, "error": f"{type(exc).__name__}: {exc}"})
                continue
            deleted.append(info.name)

    report = {
        "base_url": args.base_url,
        "mode": "apply" if args.apply else "dry-run",
        "total_collections": len(names),
        "protected_present": protected_present,
        "candidate_count": len(candidates),
        "candidate_points": sum(info.points_count or 0 for info in infos),
        "candidates": [info.__dict__ for info in infos],
        "deleted_count": len(deleted),
        "deleted": deleted,
        "failures": failures,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Qdrant base URL: {report['base_url']}")
        print(f"Mode: {report['mode']}")
        print(f"Total collections: {report['total_collections']}")
        print(f"Protected collections present: {len(protected_present)}")
        print(f"Cleanup candidates: {report['candidate_count']}")
        print(f"Candidate points: {report['candidate_points']}")
        for info in infos:
            points = "unknown" if info.points_count is None else str(info.points_count)
            print(f"- {info.name} points={points} status={info.status}")
        if args.apply:
            print(f"Deleted collections: {len(deleted)}")
        else:
            print("Dry run only. Re-run with --apply to delete candidates.")
        if failures:
            print("Failures:", file=sys.stderr)
            for failure in failures:
                print(f"- {failure['name']}: {failure['error']}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
