#!/usr/bin/env python
"""Run a bounded, read-only native acceptance matrix for code search modes.

The probe binds every request to an explicit project and repository root, then
checks the four lexical/metadata modes exposed by the public code-tools
contract. It never enables semantic fusion, refreshes an index, writes SQLite
or Qdrant, returns source snippets, or restarts BHM.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.request import Request


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.caller_auth import configured_caller_token  # noqa: E402
from blackholememory.config import settings  # noqa: E402
from blackholememory.local_endpoint_policy import MAX_RESPONSE_BYTES  # noqa: E402
from blackholememory.local_endpoint_policy import open_local_url  # noqa: E402
from blackholememory.local_endpoint_policy import read_bounded_response  # noqa: E402
from blackholememory.local_endpoint_policy import validate_local_endpoint  # noqa: E402
from blackholememory.resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS  # noqa: E402
from blackholememory.runtime_endpoints import endpoint_url  # noqa: E402


SCHEMA_VERSION = "bhm.native-code-search-acceptance.v1"
MODE_CASES: tuple[tuple[str, str], ...] = (
    ("text", "BlackHoleMemory"),
    ("path", "config"),
    ("symbol", "def"),
    ("metadata", "health"),
)


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc).replace(chr(0), '')[:300]}"


def _request(
    *,
    base_url: str,
    token: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    endpoint = validate_local_endpoint(base_url)
    body = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"{endpoint}/bhm/code-tools", data=body, method="POST", headers=headers)
    try:
        with open_local_url(request, timeout=BHM_INTERNAL_HTTP_TIMEOUT_SECONDS, endpoint=endpoint) as response:
            raw = read_bounded_response(response, limit=MAX_RESPONSE_BYTES)
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from code-tools") from exc
    payload_json = json.loads(raw.decode("utf-8"))
    if not isinstance(payload_json, dict):
        raise RuntimeError("code-tools response was not a JSON object")
    return payload_json


def _evaluate_mode(result: Mapping[str, Any], mode: str) -> dict[str, Any]:
    execution = result.get("execution") if isinstance(result.get("execution"), Mapping) else {}
    semantic = result.get("semantic_fusion") if isinstance(result.get("semantic_fusion"), Mapping) else {}
    matches = result.get("matches")
    failures: list[str] = []
    if result.get("schema_version") != "bhm.code-search.v1":
        failures.append("schema_mismatch")
    if result.get("mode") != mode:
        failures.append("mode_mismatch")
    if not isinstance(matches, list):
        failures.append("matches_not_list")
    for key in ("writes_sqlite_state", "writes_qdrant", "source_persisted", "raw_source_returned"):
        if execution.get(key) is not False:
            failures.append(f"execution_{key}")
    if execution.get("semantic_fusion") is not False or semantic.get("active") is not False:
        failures.append("semantic_fusion_not_disabled")
    if execution.get("redacted_snippets_returned") is not False:
        failures.append("snippets_returned")
    return {
        "mode": mode,
        "ok": not failures,
        "query": result.get("query"),
        "match_count": len(matches) if isinstance(matches, list) else 0,
        "snapshot_digest": result.get("snapshot_digest"),
        "search_strategy": result.get("search_strategy"),
        "failures": failures,
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "source_persisted": False,
            "raw_source_returned": False,
            "redacted_snippets_returned": False,
            "semantic_fusion": False,
        },
    }


def validate(
    *,
    base_url: str,
    token: str,
    project: str,
    root: str,
    limit: int = 5,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    normalized_root = str(Path(root).resolve())
    for mode, query in MODE_CASES:
        request_payload = {
            "operation": "code_search",
            "project": project,
            "root": normalized_root,
            "query": query,
            "search_mode": mode,
            "limit": max(1, min(int(limit), 32)),
            "offset": 0,
            "include_snippets": False,
            "semantic_fusion": False,
        }
        try:
            result = _request(base_url=base_url, token=token, payload=request_payload)
            rows.append(_evaluate_mode(result, mode))
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            rows.append(
                {
                    "mode": mode,
                    "ok": False,
                    "query": query,
                    "match_count": 0,
                    "snapshot_digest": None,
                    "search_strategy": None,
                    "failures": [_safe_error(exc)],
                    "execution": {
                        "writes_sqlite_state": False,
                        "writes_qdrant": False,
                        "source_persisted": False,
                        "raw_source_returned": False,
                        "redacted_snippets_returned": False,
                        "semantic_fusion": False,
                    },
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": bool(rows) and all(bool(row.get("ok")) for row in rows),
        "mutation": False,
        "project": project,
        "root": normalized_root,
        "base_url": validate_local_endpoint(base_url),
        "modes": rows,
        "mode_count": len(rows),
        "semantic_fusion": {
            "requested": False,
            "operator_flag_changed": False,
            "semantic_acceptance_deferred": True,
            "reason": "explicit_operator_flag_and_fresh_snapshot_required",
        },
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "index_refresh": False,
            "restart": False,
        },
        "note": "Read-only native code-search mode acceptance; explicit project/root binding is required.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=endpoint_url("bhm_api"))
    parser.add_argument("--token", default=os.getenv("BHM_CALLER_TOKEN", ""))
    parser.add_argument("--project", default="blackholememory")
    parser.add_argument("--root", default=str(settings.repo_root))
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    token = str(args.token or configured_caller_token() or "").strip()
    result = validate(
        base_url=args.base_url,
        token=token,
        project=args.project,
        root=args.root,
        limit=args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
