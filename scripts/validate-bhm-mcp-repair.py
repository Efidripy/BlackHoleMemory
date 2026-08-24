"""Run the live bounded P18.16 scoped MCP repair gate."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from bhm_runtime_endpoints import endpoint_url
from bhm_runtime_endpoints import validate_loopback_endpoint


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.caller_auth import configured_caller_token
from blackholememory.local_endpoint_policy import open_local_url
from blackholememory.local_endpoint_policy import LocalEndpointError
from blackholememory.local_endpoint_policy import read_bounded_response
from blackholememory.mcp_repair import SCHEMA_VERSION
from blackholememory.resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS


VALIDATION_SCHEMA_VERSION = "bhm.mcp.repair-validation.v1"
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
    "session_id",
    "connection_id",
    "ownership_id",
    "target",
    "path",
    "command",
    "args",
}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=endpoint_url("bhm_api"))
    parser.add_argument("--workbench-url", default=endpoint_url("workbench"))
    parser.add_argument("--compact", action="store_true")
    return parser.parse_args()


def _caller_headers(required: bool) -> dict[str, str]:
    if not required:
        return {}
    token = configured_caller_token()
    if len(token) < 32:
        raise RuntimeError("BHM_CALLER_TOKEN is unavailable")
    return {"Authorization": f"Bearer {token}"}


def _json_request(
    url: str,
    *,
    method: str = "GET",
    caller_auth: bool = False,
) -> tuple[int, dict[str, Any]]:
    parsed = urlsplit(url)
    try:
        validate_loopback_endpoint(f"{parsed.scheme}://{parsed.netloc}")
    except ValueError as exc:
        raise LocalEndpointError(str(exc)) from exc
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            "Accept": "application/json",
            "User-Agent": "BHM-P18.16-Validator/1.7.1",
            **_caller_headers(caller_auth),
        },
    )
    try:
        with open_local_url(request, timeout=BHM_INTERNAL_HTTP_TIMEOUT_SECONDS) as response:
            payload = json.loads(read_bounded_response(response).decode("utf-8"))
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
            return status, payload if isinstance(payload, dict) else {}
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(read_bounded_response(exc).decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        return int(exc.code), payload if isinstance(payload, dict) else {}


def _hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _contains_forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).casefold() in FORBIDDEN_KEYS or _contains_forbidden(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden(item) for item in value)
    return False


def main() -> int:
    args = _args()
    base_url = args.base_url.rstrip("/")
    workbench_url = args.workbench_url.rstrip("/")
    targets = [
        Path(os.environ.get("USERPROFILE", "")) / ".codex" / "config.toml",
        Path(os.environ.get("USERPROFILE", "")) / ".claude" / "settings.json",
    ]
    before_targets = {str(path): _hash(path) for path in targets}
    before_slo_status, before_slo = _json_request(f"{base_url}/bhm/health/slo")
    preview_status, preview = _json_request(
        f"{base_url}/bhm/mcp/repair/preview",
        caller_auth=True,
    )
    reprobe_status, reprobe = _json_request(
        f"{base_url}/bhm/mcp/repair/reprobe",
        caller_auth=True,
    )
    denied_status, denied = _json_request(
        f"{base_url}/bhm/mcp/repair/reconnect",
        method="POST",
        caller_auth=True,
    )
    workbench_status, workbench = _json_request(f"{workbench_url}/api/mcp-repair/preview")
    after_slo_status, after_slo = _json_request(f"{base_url}/bhm/health/slo")
    after_targets = {str(path): _hash(path) for path in targets}

    scope = preview.get("scope") if isinstance(preview.get("scope"), dict) else {}
    plan = preview.get("plan") if isinstance(preview.get("plan"), dict) else {}
    reconnect_plan = plan.get("reconnect") if isinstance(plan.get("reconnect"), dict) else {}
    reprobe_action = reprobe.get("reprobe") if isinstance(reprobe.get("reprobe"), dict) else {}
    drift_clients = preview.get("drift_clients") if isinstance(preview.get("drift_clients"), list) else []
    native_session_live = preview.get("native_session_live") is True
    transport_ready = preview.get("transport_ready") is True
    if drift_clients:
        expected_status = "client_reload_required"
        expected_reload = True
    elif native_session_live:
        expected_status = "native_session_live"
        expected_reload = False
    elif transport_ready:
        expected_status = "native_probe_required"
        expected_reload = False
    else:
        expected_status = "transport_repair_required"
        expected_reload = False
    checks = {
        "preview_http": preview_status == 200,
        "schema": preview.get("schema_version") == SCHEMA_VERSION,
        "bounded_read_only": preview.get("read_only") is True and preview.get("bounded") is True,
        "no_live_writes": preview.get("writes_live_state") is False,
        "bhm_only_scope": scope.get("mode") == "bhm-only" and set(scope.get("server_ids") or []) == {"bhm"},
        "all_active_clients": scope.get("clients") == ["codex", "claude"],
        "foreign_servers_untouched": scope.get("foreign_servers_touched") is False and scope.get("foreign_servers_untouched") is True and preview.get("foreign_servers_untouched") is True,
        "adapter_inventory_bounded": len(preview.get("adapters") or []) == 2 and all("target" not in row and "path" not in row for row in preview.get("adapters") or []),
        "client_restart_boundary": reconnect_plan.get("restart_api_available") is False and reconnect_plan.get("auto_repair") is False,
        "reconnect_disposition_truth": (
            reconnect_plan.get("status") == expected_status
            and reconnect_plan.get("client_reload_required") is expected_reload
        ),
        "native_probe_before_reload": (
            expected_status not in {"native_probe_required", "transport_repair_required"}
            or not str(preview.get("recommendation") or "").startswith("reload")
        ),
        "reprobe_http": reprobe_status == 200 and reprobe_action.get("status") == "complete" and reprobe.get("writes_live_state") is False,
        "mutating_route_fail_closed": denied_status == 403 and denied.get("detail", {}).get("code") == "admin_capability_required",
        "workbench_proxy": (
            workbench_status == 200
            and workbench.get("schema_version") == SCHEMA_VERSION
            and workbench.get("writes_live_state") is False
            and ((workbench.get("plan") or {}).get("reconnect") or {}).get("status") == expected_status
        ),
        "privacy": not _contains_forbidden(preview) and not _contains_forbidden(reprobe) and not _contains_forbidden(workbench),
        "targets_unchanged": before_targets == after_targets,
        "slo_before": before_slo_status == 200 and before_slo.get("status") == "healthy",
        "slo_after": after_slo_status == 200 and after_slo.get("status") == "healthy" and int((after_slo.get("observed") or {}).get("outbox", {}).get("failed", 0)) == 0,
        "slo_failed_unchanged": int((before_slo.get("observed") or {}).get("outbox", {}).get("failed", 0)) == int((after_slo.get("observed") or {}).get("outbox", {}).get("failed", 0)),
    }
    result = {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "ok": all(checks.values()),
        "checks": checks,
        "repair": {
            "repair_id_present": bool(preview.get("repair_id")),
            "scope": scope.get("mode"),
            "clients": scope.get("clients"),
            "reconnect_status": reconnect_plan.get("status"),
            "client_reload_required": reconnect_plan.get("client_reload_required"),
            "rollback_status": (plan.get("rollback") or {}).get("status"),
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
    raise SystemExit(main())
