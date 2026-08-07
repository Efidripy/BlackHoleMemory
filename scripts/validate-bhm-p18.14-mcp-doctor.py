"""Run the live bounded P18.14 MCP Doctor gate."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.request
from typing import Any

from bhm_runtime_endpoints import endpoint_url


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


def _ensure_project_runtime() -> None:
    """Re-exec with the project interpreter when host Python lacks psutil."""

    try:
        import psutil  # noqa: F401

        return
    except ImportError:
        pass

    current = Path(sys.executable).resolve()
    candidates = (
        REPO_ROOT / ".venv" / "Scripts" / "python.exe",
        REPO_ROOT / "venv" / "Scripts" / "python.exe",
        REPO_ROOT / ".venv" / "bin" / "python",
        REPO_ROOT / "venv" / "bin" / "python",
    )
    for candidate in candidates:
        if not candidate.is_file() or candidate.resolve() == current:
            continue
        try:
            os.execv(str(candidate.resolve()), [str(candidate.resolve()), str(Path(__file__).resolve()), *sys.argv[1:]])
        except OSError:
            continue


_ensure_project_runtime()
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.mcp_final_gate import EXPECTED_CLIENTS
from blackholememory.mcp_doctor import DoctorConfig
from blackholememory.mcp_doctor import VALIDATION_SCHEMA_VERSION
from blackholememory.mcp_doctor import run_doctor
from blackholememory.local_endpoint_policy import open_local_url
from blackholememory.local_endpoint_policy import read_bounded_response
from blackholememory.resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS


FORBIDDEN_KEYS = {
    "prompt",
    "prompts",
    "content",
    "tool_arguments",
    "arguments",
    "environment",
    "env",
    "secret",
    "secrets",
    "token",
    "lease_token",
    "client_id",
    "session_id",
    "ownership_id",
    "registrations",
}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=endpoint_url("bhm_api"))
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--compact", action="store_true")
    return parser.parse_args()


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "application/json", "User-Agent": "BHM-P18.14-Validator/1.7.1"},
    )
    with open_local_url(request, timeout=BHM_INTERNAL_HTTP_TIMEOUT_SECONDS) as response:
        status_value = getattr(response, "status", None)
        status = int(status_value if status_value is not None else response.getcode())
        if status != 200:
            raise RuntimeError(f"unexpected HTTP status {status}")
        value = json.loads(read_bounded_response(response, limit=128 * 1024).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"non-object response from {path}")
    return value


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() == "privacy":
                continue
            if str(key).casefold() in FORBIDDEN_KEYS or _contains_forbidden_key(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def main() -> int:
    args = _args()
    before_slo = _get_json(args.base_url, "/bhm/health/slo")
    report = run_doctor(DoctorConfig(base_url=args.base_url, repo_root=REPO_ROOT, timeout_seconds=args.timeout_seconds))
    after_slo = _get_json(args.base_url, "/bhm/health/slo")
    configured = report.get("configured_sources") if isinstance(report.get("configured_sources"), dict) else {}
    duplicates = report.get("duplicates") if isinstance(report.get("duplicates"), dict) else {}
    runtime = report.get("runtime") if isinstance(report.get("runtime"), dict) else {}
    pipe = report.get("pipe") if isinstance(report.get("pipe"), dict) else {}
    protocol = report.get("protocol") if isinstance(report.get("protocol"), dict) else {}
    catalog = report.get("catalog") if isinstance(report.get("catalog"), dict) else {}
    leases = report.get("leases") if isinstance(report.get("leases"), dict) else {}
    ownership = report.get("process_ownership") if isinstance(report.get("process_ownership"), dict) else {}
    next_action = report.get("next_action") if isinstance(report.get("next_action"), dict) else {}
    configured_sources = configured.get("sources") if isinstance(configured.get("sources"), list) else []
    configured_clients = {
        str(item.get("client"))
        for item in configured_sources
        if isinstance(item, dict)
    }
    checks = {
        "schema": report.get("schema_version") == "bhm.mcp.doctor.v1",
        "bounded_read_only": report.get("read_only") is True and report.get("bounded") is True,
        "no_live_writes": report.get("writes_live_state") is False,
        "configured_sources": (
            configured.get("status") == "aligned"
            and configured.get("source_count") == len(EXPECTED_CLIENTS)
            and configured.get("configured_count") == len(EXPECTED_CLIENTS)
            and configured_clients == set(EXPECTED_CLIENTS)
        ),
        "duplicate_truth": duplicates.get("status") in {"clean", "retained_duplicates"} and duplicates.get("active_conflict") is False,
        "runtime": runtime.get("ready") is True and runtime.get("cutover") is True and runtime.get("slo") == "healthy",
        "pipe": pipe.get("connected") is True,
        "protocol": protocol.get("ok") is True and protocol.get("initialize_ok") is True and protocol.get("shutdown_ok") is True,
        "catalog": catalog.get("usable") is True and catalog.get("tool_count") == 12 and len(str(catalog.get("schema_hash") or "")) == 64 and len(str(catalog.get("generation") or "")) == 64,
        "leases_detached": leases.get("status") == "detached" and leases.get("active_count") == 0,
        "ownership": ownership.get("status") == "clean" and ownership.get("invalid_record_count") == 0 and ownership.get("orphaned_count") == 0 and ownership.get("broad_process_kill") is False,
        "next_action_bounded": next_action.get("severity") in {"none", "medium"} and bool(next_action.get("reason_code")) and bool(next_action.get("action")),
        "privacy": not _contains_forbidden_key(report),
        "slo_after": after_slo.get("status") == "healthy" and int((after_slo.get("observed") or {}).get("outbox", {}).get("failed", 0)) == 0,
        "slo_before_after_no_failed_writes": int((before_slo.get("observed") or {}).get("outbox", {}).get("failed", 0)) == int((after_slo.get("observed") or {}).get("outbox", {}).get("failed", 0)),
    }
    result = {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "ok": all(checks.values()),
        "checks": checks,
        "doctor": {
            "status": report.get("status"),
            "ok": report.get("ok"),
            "next_action": next_action,
            "configured_sources": configured.get("source_count"),
            "duplicate_status": duplicates.get("status"),
            "catalog_tools": catalog.get("tool_count"),
            "catalog_schema_hash": bool(catalog.get("schema_hash")),
            "catalog_generation": bool(catalog.get("generation")),
            "active_leases": leases.get("active_count"),
            "ownership_records": ownership.get("record_count"),
        },
        "slo": {
            "before": before_slo.get("status"),
            "after": after_slo.get("status"),
            "outbox_failed_before": int((before_slo.get("observed") or {}).get("outbox", {}).get("failed", 0)),
            "outbox_failed_after": int((after_slo.get("observed") or {}).get("outbox", {}).get("failed", 0)),
        },
        "writes_live_state": False,
    }
    output = json.dumps(result, ensure_ascii=False, separators=(",", ":")) if args.compact else json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(output)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema_version": VALIDATION_SCHEMA_VERSION, "ok": False, "error_code": type(exc).__name__.casefold()}))
        raise SystemExit(1)
