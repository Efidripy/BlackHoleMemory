"""Validate bounded MCP catalog identity across ephemeral HTTP attaches.

This probe is deliberately read-only with respect to BHM authority.  It opens
short-lived Streamable HTTP sessions, performs only ``initialize``,
``notifications/initialized`` and ``tools/list``, verifies the redacted session
registry catalog hash, and releases each session.  The session lifecycle is
ephemeral transport state; SQLite, Qdrant, worktree and client configuration
are never written.

The transport registry's ``catalog_hash`` is the digest of the raw ``tools``
array.  ``mcp_catalog_contract.schema_hash`` is a different, canonical digest
that also binds the negotiated protocol version and canonicalized tool
schemas.  Both are reported explicitly so callers do not mistake the two
domains for schema drift.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.mcp_catalog_contract import CatalogContractError
from blackholememory.mcp_catalog_contract import build_catalog_contract
from blackholememory.mcp_doctor import _get_json
from blackholememory.mcp_doctor import _http_mcp_request
from blackholememory.mcp_protocol_contract import CURRENT_PROTOCOL_VERSION
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.runtime_endpoints import endpoint_url


SCHEMA_VERSION = "bhm.mcp.catalog-lifecycle-validation.v1"
MAX_PROBES = 25
DEFAULT_PROBES = 3
MAX_ISSUES = 32


def _transport_catalog_hash(tools: Any) -> str | None:
    """Match the Streamable HTTP registry's raw catalog digest exactly."""

    if not isinstance(tools, list):
        return None
    payload = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _session_ref(session_id: str) -> str:
    """Return the same redacted session reference used by the runtime."""

    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:12]


def _status_payload(base_url: str, timeout_seconds: float) -> dict[str, Any]:
    payload, error = _get_json(base_url, "/bhm/mcp/http/status", timeout_seconds=timeout_seconds)
    if payload is None:
        raise RuntimeError(error or "mcp_http_status_unavailable")
    return payload


def _status_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    sessions = payload.get("sessions") if isinstance(payload.get("sessions"), Mapping) else {}
    rows = sessions.get("sessions") if isinstance(sessions.get("sessions"), list) else []
    return [row for row in rows if isinstance(row, Mapping)]


def _row_for_ref(payload: Mapping[str, Any], session_ref: str) -> Mapping[str, Any] | None:
    return next((row for row in _status_rows(payload) if str(row.get("session_ref") or "") == session_ref), None)


def _extract_session_id(headers: Mapping[str, str]) -> str:
    return next((str(value or "").strip() for key, value in headers.items() if key.casefold() == "mcp-session-id"), "")


def _probe_once(base_url: str, timeout_seconds: float, ordinal: int) -> dict[str, Any]:
    endpoint = f"{base_url.rstrip('/')}/mcp"
    session_id = ""
    session_ref = ""
    before_payload = _status_payload(base_url, timeout_seconds)
    before_refs = {str(row.get("session_ref") or "") for row in _status_rows(before_payload)}
    initialize: dict[str, Any] = {}
    catalog_response: dict[str, Any] = {}
    notification_status: int | None = None
    delete_status: int | None = None
    status_row: Mapping[str, Any] | None = None
    status_hash: str | None = None
    cleanup_ok = False
    try:
        init_status, initialize, init_headers = _http_mcp_request(
            endpoint,
            {
                "jsonrpc": "2.0",
                "id": f"catalog-lifecycle-init-{ordinal}",
                "method": "initialize",
                "params": {
                    "protocolVersion": CURRENT_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "BHM Catalog Lifecycle Validator", "version": "1.7.1"},
                },
            },
            timeout_seconds=timeout_seconds,
        )
        session_id = _extract_session_id(init_headers)
        session_ref = _session_ref(session_id) if session_id else ""
        if init_status != 200 or not session_id or not isinstance(initialize.get("result"), Mapping):
            raise RuntimeError("initialize_failed")

        notification_status, _, _ = _http_mcp_request(
            endpoint,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            timeout_seconds=timeout_seconds,
            session_id=session_id,
        )
        if notification_status != 202:
            raise RuntimeError("initialized_notification_failed")

        catalog_status, catalog_response, _ = _http_mcp_request(
            endpoint,
            {"jsonrpc": "2.0", "id": f"catalog-lifecycle-list-{ordinal}", "method": "tools/list", "params": {}},
            timeout_seconds=timeout_seconds,
            session_id=session_id,
        )
        if catalog_status != 200 or not isinstance(catalog_response.get("result"), Mapping):
            raise RuntimeError("tools_list_failed")
        tools = catalog_response["result"].get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("tools_list_missing_array")
        status_after_catalog = _status_payload(base_url, timeout_seconds)
        status_row = _row_for_ref(status_after_catalog, session_ref)
        status_hash = str(status_row.get("catalog_hash") or "") if status_row else None
        transport_hash = _transport_catalog_hash(tools)
        try:
            contract = build_catalog_contract(initialize, catalog_response).as_dict()
        except CatalogContractError as exc:
            raise RuntimeError("catalog_contract_invalid") from exc
        contract_hash = str(contract.get("schema_hash") or "")
        generation = str(contract.get("generation") or "")
        result = {
            "ordinal": ordinal,
            "ok": bool(
                contract.get("usable") is True
                and transport_hash
                and status_hash == transport_hash
                and len(contract_hash) == 64
                and len(generation) == 64
            ),
            "protocol_version": str(contract.get("protocol_version") or "")[:32],
            "server_id": str(contract.get("server", {}).get("id") or "")[:32]
            if isinstance(contract.get("server"), Mapping)
            else "",
            "surface": str(contract.get("server", {}).get("surface") or "")[:32]
            if isinstance(contract.get("server"), Mapping)
            else "",
            "tool_count": min(int(contract.get("tool_count") or 0), 128),
            "expected_core_tool_count": len(CORE_TOOL_NAMES),
            "transport_catalog_hash": transport_hash,
            "registry_catalog_hash": status_hash,
            "schema_hash": contract_hash,
            "generation": generation,
            "registry_hash_bound": bool(status_hash and status_hash == transport_hash),
            "digest_domains": {
                "transport_catalog_hash": "sha256(raw tools array)",
                "schema_hash": "sha256(protocol version plus canonicalized tool schemas)",
            },
            "notification_status": notification_status,
            "delete_status": None,
            "cleanup_ok": False,
            "error_code": None,
        }
    except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "ordinal": ordinal,
            "ok": False,
            "transport_catalog_hash": _transport_catalog_hash(
                (catalog_response.get("result") or {}).get("tools")
                if isinstance(catalog_response.get("result"), Mapping)
                else None
            ),
            "registry_catalog_hash": status_hash,
            "schema_hash": None,
            "generation": None,
            "registry_hash_bound": False,
            "notification_status": notification_status,
            "delete_status": None,
            "cleanup_ok": False,
            "error_code": str(exc).split(":", 1)[0][:80] or "probe_failed",
        }
    finally:
        if session_id:
            try:
                delete_status, _, _ = _http_mcp_request(
                    endpoint,
                    None,
                    timeout_seconds=timeout_seconds,
                    session_id=session_id,
                    method="DELETE",
                )
            except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError):
                delete_status = None
            try:
                final_payload = _status_payload(base_url, timeout_seconds)
                cleanup_ok = _row_for_ref(final_payload, session_ref) is None
                # A baseline session may exist; only this probe's redacted ref
                # is relevant, and raw session IDs never enter the report.
                cleanup_ok = cleanup_ok and session_ref not in before_refs
            except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError):
                cleanup_ok = False
        result["delete_status"] = delete_status
        result["cleanup_ok"] = cleanup_ok
        result["ok"] = bool(result.get("ok") and delete_status in {200, 202, 204} and cleanup_ok)
    return result


def validate_catalog_lifecycle(
    *,
    base_url: str = endpoint_url("bhm_api"),
    probes: int = DEFAULT_PROBES,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Run repeated bounded attaches and return a redacted validation report."""

    if not 1 <= int(probes) <= MAX_PROBES:
        raise ValueError(f"probes must be between 1 and {MAX_PROBES}")
    if not 1.0 <= float(timeout_seconds) <= 120.0:
        raise ValueError("timeout_seconds must be between 1 and 120")
    parsed = urlsplit(str(base_url))
    if parsed.username or parsed.password or parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an HTTP URL without credentials")
    rows = [_probe_once(str(base_url).rstrip("/"), float(timeout_seconds), index) for index in range(1, int(probes) + 1)]
    successful = [row for row in rows if row.get("ok") is True]
    schema_hashes = sorted({str(row.get("schema_hash") or "") for row in successful if row.get("schema_hash")})
    generations = sorted({str(row.get("generation") or "") for row in successful if row.get("generation")})
    transport_hashes = sorted(
        {str(row.get("transport_catalog_hash") or "") for row in successful if row.get("transport_catalog_hash")}
    )
    registry_hashes = sorted(
        {str(row.get("registry_catalog_hash") or "") for row in successful if row.get("registry_catalog_hash")}
    )
    issues: list[str] = []
    if len(successful) != len(rows):
        issues.append("one_or_more_attach_probes_failed")
    if len(schema_hashes) > 1 or len(generations) > 1 or len(transport_hashes) > 1 or len(registry_hashes) > 1:
        issues.append("catalog_digest_drift_across_ephemeral_attaches")
    tool_counts = sorted({int(row.get("tool_count") or 0) for row in successful})
    if tool_counts != [len(CORE_TOOL_NAMES)]:
        issues.append("live_tool_count_differs_from_allowlist")
    if any(row.get("registry_hash_bound") is not True for row in rows):
        issues.append("registry_catalog_hash_not_bound_to_raw_catalog")
    if any(row.get("cleanup_ok") is not True for row in rows):
        issues.append("ephemeral_session_cleanup_failed")
    if schema_hashes and transport_hashes and schema_hashes[0] == transport_hashes[0]:
        issues.append("digest_domain_collision")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not issues,
        "bounded": True,
        "read_only": True,
        "writes_live_state": False,
        "ephemeral_session_state_only": True,
        "probe_count": len(rows),
        "max_probe_count": MAX_PROBES,
        "runtime": {
            "base_url": str(base_url).rstrip("/"),
            "transport": "streamable_http",
            "protocol_version": CURRENT_PROTOCOL_VERSION,
            "canonical_server_id": "bhm",
        },
        "catalog": {
            "schema_hashes": schema_hashes,
            "generations": generations,
            "transport_catalog_hashes": transport_hashes,
            "registry_catalog_hashes": registry_hashes,
            "tool_counts": tool_counts,
            "expected_core_tool_count": len(CORE_TOOL_NAMES),
            "digest_domains_distinct": bool(
                schema_hashes and transport_hashes and schema_hashes[0] != transport_hashes[0]
            ),
        },
        "checks": {
            "all_probes_usable": len(successful) == len(rows),
            "schema_hash_stable": len(schema_hashes) <= 1 and bool(schema_hashes),
            "generation_stable": len(generations) <= 1 and bool(generations),
            "transport_catalog_hash_stable": len(transport_hashes) <= 1 and bool(transport_hashes),
            "tool_count_matches_allowlist": tool_counts == [len(CORE_TOOL_NAMES)],
            "registry_hash_bound": all(row.get("registry_hash_bound") is True for row in rows),
            "ephemeral_cleanup": all(row.get("cleanup_ok") is True for row in rows),
            "digest_domains_distinct": bool(
                schema_hashes and transport_hashes and schema_hashes[0] != transport_hashes[0]
            ),
        },
        "issues": issues[:MAX_ISSUES],
        "probes": rows,
    }


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=endpoint_url("bhm_api"))
    parser.add_argument("--probes", type=int, default=DEFAULT_PROBES)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def _write_report(path: Path, output: str) -> None:
    replace_bytes_safely(path, (output + "\n").encode("utf-8"))


def main() -> int:
    args = _args()
    report = validate_catalog_lifecycle(
        base_url=args.base_url,
        probes=args.probes,
        timeout_seconds=args.timeout_seconds,
    )
    output = json.dumps(
        report,
        ensure_ascii=False,
        separators=(",", ":") if args.compact else None,
        indent=None if args.compact else 2,
        sort_keys=not args.compact,
    )
    if args.output is not None:
        _write_report(args.output, output)
    print(output)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "ok": False, "error_code": type(exc).__name__.casefold()}))
        raise SystemExit(1)
