"""Scoped, fail-closed MCP repair orchestration.

The BHM runtime can inspect and repair its own adapter registrations, but it
cannot control a closed Codex/Claude process.  This module therefore keeps the
workflow explicit: preview the BHM-only scope, optionally apply only the
managed BHM adapter entries after a canary, re-probe the runtime, and expose an
exact rollback handle.  A missing client restart API is reported as a required
client reload; it is never promoted to an automatic reconnect.
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .filesystem_boundaries import assert_safe_path
from .filesystem_boundaries import replace_bytes_safely

SCHEMA_VERSION = "bhm.mcp.repair.v1"
MAX_CLIENTS = 2
MAX_PLANS = 32
SUPPORTED_CLIENTS = ("codex", "claude")
BHM_SERVER_IDS = frozenset(("bhm",))
REPAIR_ID_RE = re.compile(r"^mcp-repair-[0-9a-f]{16}$")
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_PLAN_LOCK = threading.RLock()
_GENERATOR_CACHE: dict[str, Any] = {}


class McpRepairError(ValueError):
    """Raised when a scoped repair request cannot be proven safe."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_label(value: Any, *, fallback: str = "unknown", maximum: int = 96) -> str:
    candidate = str(value or "").strip()
    if len(candidate) > maximum or not SAFE_LABEL_RE.fullmatch(candidate):
        return fallback
    return candidate


def _new_repair_id() -> str:
    return f"mcp-repair-{uuid.uuid4().hex[:16]}"


def _normalize_clients(clients: Any) -> list[str]:
    if clients is None:
        return list(SUPPORTED_CLIENTS)
    if isinstance(clients, str):
        values = [clients]
    elif isinstance(clients, (list, tuple, set, frozenset)):
        values = list(clients)
    else:
        raise McpRepairError("clients must be a bounded list")
    if not values:
        return list(SUPPORTED_CLIENTS)
    normalized = []
    for value in values:
        client = str(value or "").strip().lower()
        if client not in SUPPORTED_CLIENTS:
            raise McpRepairError(f"unsupported BHM client: {client or 'empty'}")
        if client not in normalized:
            normalized.append(client)
    if len(normalized) > MAX_CLIENTS:
        raise McpRepairError("client scope exceeds the bounded BHM adapter set")
    return [client for client in SUPPORTED_CLIENTS if client in normalized]


def _scope(clients: list[str]) -> dict[str, Any]:
    return {
        "mode": "bhm-only",
        "clients": list(clients),
        "server_ids": sorted(BHM_SERVER_IDS),
        "foreign_servers_touched": False,
        "foreign_servers_untouched": True,
    }


def _plan_root(repo_root: Path) -> Path:
    return Path(repo_root).resolve() / ".runtime" / "mcp-repair" / "plans"


def _plan_path(repo_root: Path, repair_id: str) -> Path:
    if not REPAIR_ID_RE.fullmatch(repair_id):
        raise McpRepairError("invalid repair_id")
    return _plan_root(repo_root) / f"{repair_id}.json"


def _write_plan(repo_root: Path, repair_id: str, payload: Mapping[str, Any]) -> None:
    path = _plan_path(repo_root, repair_id)
    encoded = (json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with _PLAN_LOCK:
        replace_bytes_safely(path, encoded)
        # lgtm [py/path-injection]
        plans = []
        for item in path.parent.glob("mcp-repair-*.json"):
            assert_safe_path(item)
            plans.append(item)
        plans.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for stale in plans[MAX_PLANS:]:
            assert_safe_path(stale)
            stale.unlink(missing_ok=True)


def _read_plan(repo_root: Path, repair_id: str) -> dict[str, Any] | None:
    path = _plan_path(repo_root, repair_id)
    try:
        assert_safe_path(path)
        # lgtm [py/path-injection]
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _backup_root(repo_root: Path) -> Path:
    return Path(repo_root).resolve().parent.parent / "workspace" / "runtime" / "logs" / "mcp-adapters" / "backups"


def _safe_backup_dir(repo_root: Path, value: Any) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if not Path(raw).is_absolute():
        raise McpRepairError("rollback backup must use an absolute path")
    root_name = os.path.realpath(os.fspath(_backup_root(repo_root)))
    candidate_name = os.path.realpath(os.path.expanduser(raw))
    try:
        contained = os.path.commonpath((root_name, candidate_name)) == root_name
    except ValueError as exc:
        raise McpRepairError("rollback backup is outside the BHM adapter backup root") from exc
    if not contained:
        raise McpRepairError("rollback backup is outside the BHM adapter backup root")
    root = Path(root_name)
    assert_safe_path(root, reject_hardlink_target=False)
    candidate = Path(candidate_name)
    assert_safe_path(candidate, reject_hardlink_target=False)
    if candidate.exists() and not candidate.is_dir():
        raise McpRepairError("rollback backup path is not a directory")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contained_path(root: Path, candidate: Path) -> bool:
    root_name = os.path.realpath(os.fspath(root))
    candidate_name = os.path.realpath(os.fspath(candidate))
    try:
        return os.path.commonpath((root_name, candidate_name)) == root_name
    except ValueError as exc:
        raise McpRepairError("rollback manifest path crosses filesystem roots") from exc


def _validate_backup_scope(repo_root: Path, backup_dir: Path, clients: list[str]) -> None:
    """Prove a rollback manifest contains only the selected BHM targets."""

    manifest_path = backup_dir / "manifest.json"
    assert_safe_path(backup_dir, reject_hardlink_target=False)
    if not backup_dir.is_dir():
        raise McpRepairError("rollback backup directory is missing")
    assert_safe_path(manifest_path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise McpRepairError("rollback manifest is unreadable") from exc
    generator, adapters = _adapter_context(Path(repo_root), clients)
    if not isinstance(payload, Mapping) or payload.get("schema") != getattr(generator, "SCHEMA", None):
        raise McpRepairError("rollback manifest schema is invalid")
    records = payload.get("records")
    if not isinstance(records, list) or not records or len(records) > MAX_CLIENTS:
        raise McpRepairError("rollback manifest has an invalid bounded record set")
    expected_targets = {client: os.path.realpath(os.fspath(adapter.target)) for client, adapter in adapters.items()}
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise McpRepairError("rollback manifest has an invalid record")
        client = str(record.get("client") or "").strip().lower()
        target = str(record.get("target") or "").strip()
        if client not in expected_targets or not target or os.path.realpath(target) != expected_targets[client]:
            raise McpRepairError("rollback manifest escapes the BHM-only target scope")
        target_path = Path(target)
        assert_safe_path(target_path)
        backup_path = Path(str(record.get("backup") or ""))
        if not _contained_path(backup_dir, backup_path):
            raise McpRepairError("rollback manifest backup escapes its manifest directory")
        existed = bool(record.get("existed"))
        expected_hash = str(record.get("sha256_before") or "").strip().lower()
        if existed:
            assert_safe_path(backup_path)
            if not backup_path.is_file():
                raise McpRepairError("rollback manifest backup is not a regular file")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or _sha256_file(backup_path) != expected_hash:
                raise McpRepairError("rollback manifest backup hash mismatch")
        elif backup_path.exists():
            raise McpRepairError("rollback manifest unexpectedly contains a backup for a missing target")
        seen.add(client)
    if len(seen) != len(records) or seen != set(clients):
        raise McpRepairError("rollback manifest contains duplicate client records")


def _load_generator(repo_root: Path) -> Any:
    script = Path(repo_root).resolve() / "scripts" / "generate-bhm-mcp-adapters.py"
    if not script.is_file():
        raise McpRepairError("BHM adapter generator is unavailable")
    cache_key = str(script)
    with _PLAN_LOCK:
        cached = _GENERATOR_CACHE.get(cache_key)
        if cached is not None:
            return cached
    spec = importlib.util.spec_from_file_location("bhm_mcp_adapter_generator_runtime", script)
    if spec is None or spec.loader is None:
        raise McpRepairError("BHM adapter generator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with _PLAN_LOCK:
        _GENERATOR_CACHE[cache_key] = module
    return module


def _adapter_context(repo_root: Path, clients: list[str]) -> tuple[Any, dict[str, Any]]:
    generator = _load_generator(repo_root)
    manifest_path = Path(repo_root).resolve() / "config" / "mcp-registration.json"
    try:
        _manifest, contract = generator._contract(manifest_path, Path(repo_root).resolve())
        adapters = generator._adapters(_manifest, contract, Path(repo_root).resolve())
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise McpRepairError("BHM adapter contract is unavailable") from exc
    selected: dict[str, Any] = {}
    for client in clients:
        adapter = adapters.get(client)
        if adapter is None:
            raise McpRepairError(f"BHM adapter is missing: {client}")
        server_id = str(adapter.server_id).strip().lower()
        if server_id not in BHM_SERVER_IDS:
            raise McpRepairError(f"adapter {client} is outside the BHM-only scope")
        selected[client] = adapter
    return generator, selected


def _issue_code(value: Any) -> str:
    issue = str(value or "").strip().lower()
    if issue.startswith("extra_drift:"):
        return "extra_drift"
    if issue in {"target_missing", "identity_drift"}:
        return issue
    if "malformed" in issue:
        return "malformed_target"
    if "missing" in issue:
        return "managed_entry_missing"
    return "adapter_check_failed"


def _adapter_record(generator: Any, adapter: Any, repo_root: Path) -> dict[str, Any]:
    try:
        raw = generator._check_adapter(adapter, repo_root=Path(repo_root).resolve())
    except Exception:  # pragma: no cover - defensive boundary around a local tool
        raw = {"ok": False, "exists": False, "issues": ["adapter_check_failed"]}
    return {
        "client": _safe_label(getattr(adapter, "client", "unknown"), maximum=24),
        "server_id": _safe_label(getattr(adapter, "server_id", "unknown"), maximum=32),
        "format": _safe_label(getattr(adapter, "format", "unknown"), maximum=12),
        "managed_scope": _safe_label(getattr(adapter, "managed_scope", "unknown"), maximum=64),
        "reload_action": _safe_label(getattr(adapter, "reload_action", "unknown"), maximum=64),
        "exists": bool(raw.get("exists")),
        "ok": bool(raw.get("ok")),
        "issues": [_issue_code(item) for item in list(raw.get("issues") or [])[:8]],
    }


def _adapter_snapshot(repo_root: Path, clients: list[str]) -> tuple[list[dict[str, Any]], Any, dict[str, Any]]:
    generator, adapters = _adapter_context(repo_root, clients)
    records = [_adapter_record(generator, adapters[client], repo_root) for client in clients]
    return records, generator, adapters


def _runtime_healthy(panel: Mapping[str, Any]) -> bool:
    runtime = panel.get("runtime") if isinstance(panel.get("runtime"), Mapping) else {}
    return (
        runtime.get("state") == "healthy"
        and runtime.get("ready") is True
        and runtime.get("cutover") is True
        and runtime.get("slo") == "healthy"
    )


def _transport_ready(panel: Mapping[str, Any]) -> bool:
    rest = panel.get("rest_degraded") if isinstance(panel.get("rest_degraded"), Mapping) else {}
    return rest.get("transport_ready") is True


def _client_actions(
    records: list[Mapping[str, Any]],
    *,
    force_reload: bool = False,
    native_live: bool = False,
    transport_ready: bool = False,
) -> list[dict[str, Any]]:
    actions = []
    for record in records[:MAX_CLIENTS]:
        reload_required = bool(force_reload or not record.get("ok"))
        if reload_required:
            reason_code = "adapter_reload_required"
        elif native_live:
            reason_code = "native_session_live"
        elif transport_ready:
            reason_code = "native_probe_required"
        else:
            reason_code = "transport_repair_required"
        actions.append(
            {
                "client": _safe_label(record.get("client"), maximum=24),
                "reload_action": _safe_label(record.get("reload_action"), maximum=64),
                "restart_api_available": False,
                "auto_repair": False,
                "client_reload_required": reload_required,
                "reason_code": reason_code,
            }
        )
    return actions


def _sanitize_canary(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = raw if isinstance(raw, Mapping) else {}
    clients = []
    for record in list(payload.get("clients") or [])[:MAX_CLIENTS]:
        if not isinstance(record, Mapping):
            continue
        clients.append(
            {
                "client": _safe_label(record.get("client"), maximum=24),
                "ok": bool(record.get("ok")),
                "issues": [_issue_code(item) for item in list(record.get("issues") or [])[:8]],
            }
        )
    rollback = payload.get("rollback") if isinstance(payload.get("rollback"), Mapping) else {}
    return {
        "mode": "canary",
        "ok": bool(payload.get("ok")),
        "writes_live_state": False,
        "client_count": len(clients),
        "clients": clients,
        "rollback": {"attempted": bool(rollback.get("attempted")), "ok": bool(rollback.get("ok"))},
    }


def _sanitize_apply(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = raw if isinstance(raw, Mapping) else {}
    backup = payload.get("backup") if isinstance(payload.get("backup"), Mapping) else {}
    records = list(backup.get("records") or [])[:MAX_CLIENTS]
    return {
        "mode": "apply",
        "ok": bool(payload.get("ok")),
        "writes_live_state": bool(payload.get("writes_live_state")),
        "backup_record_count": len(records),
        "backup_created": bool(payload.get("backup_dir")),
    }


def _sanitize_rollback(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = raw if isinstance(raw, Mapping) else {}
    rollback = payload.get("rollback") if isinstance(payload.get("rollback"), Mapping) else {}
    records = []
    for record in list(rollback.get("records") or [])[:MAX_CLIENTS]:
        if not isinstance(record, Mapping):
            continue
        records.append(
            {
                "client": _safe_label(record.get("client"), maximum=24),
                "restored": bool(record.get("restored")),
            }
        )
    return {
        "mode": "rollback",
        "ok": bool(payload.get("ok")),
        "writes_live_state": bool(payload.get("writes_live_state")),
        "attempted": bool(rollback.get("attempted")),
        "records": records,
    }


def _base_result(
    *,
    operation: str,
    repair_id: str,
    clients: list[str],
    panel: Mapping[str, Any],
    adapters: list[dict[str, Any]],
) -> dict[str, Any]:
    connected = panel.get("connected") if isinstance(panel.get("connected"), Mapping) else {}
    native_live = connected.get("state") == "attached" and int(connected.get("attached_count") or 0) > 0
    transport_ready = native_live or _transport_ready(panel)
    drift = [item["client"] for item in adapters if not item.get("ok")]
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "repair_id": repair_id,
        "read_only": True,
        "writes_live_state": False,
        "bounded": True,
        "scope": _scope(clients),
        "panel": dict(panel),
        "adapters": adapters,
        "drift_clients": drift,
        "client_actions": _client_actions(
            adapters,
            force_reload=bool(drift),
            native_live=native_live,
            transport_ready=transport_ready,
        ),
        "native_session_live": native_live,
        "transport_ready": transport_ready,
        "native_probe_required": transport_ready and not native_live,
        "foreign_servers_untouched": True,
    }


def build_repair_preview(
    *,
    repo_root: Path,
    panel: Mapping[str, Any],
    clients: Any = None,
    repair_id: str | None = None,
) -> dict[str, Any]:
    selected = _normalize_clients(clients)
    identifier = repair_id or _new_repair_id()
    adapters, _generator, _selected_adapters = _adapter_snapshot(Path(repo_root), selected)
    result = _base_result(operation="preview", repair_id=identifier, clients=selected, panel=panel, adapters=adapters)
    reload_required = any(item["client_reload_required"] for item in result["client_actions"])
    if reload_required:
        reconnect_status = "client_reload_required"
        recommendation = "apply the reviewed BHM adapter repair, then reload only the affected MCP client"
    elif result["native_session_live"]:
        reconnect_status = "native_session_live"
        recommendation = "native BHM session is live; verify this client with a native BHM tool call"
    elif result["transport_ready"]:
        reconnect_status = "native_probe_required"
        recommendation = (
            "invoke a native BHM tool to establish or recover the Streamable HTTP session; "
            "reload only if that probe fails while runtime/config are healthy"
        )
    else:
        reconnect_status = "transport_repair_required"
        recommendation = (
            "repair the canonical BHM transport/runtime and re-probe; reload only after a healthy native probe fails"
        )
    result.update(
        {
            "ok": True,
            "plan": {
                "preview": {"status": "ready", "read_only": True},
                "reconnect": {
                    "status": reconnect_status,
                    "target_scope": "BHM registration and native lease only",
                    "restart_api_available": False,
                    "auto_repair": False,
                    "client_reload_required": reload_required,
                    "native_probe_required": reconnect_status == "native_probe_required",
                    "transport_repair_required": reconnect_status == "transport_repair_required",
                },
                "reprobe": {"status": "available", "read_only": True},
                "rollback": {
                    "status": "available_after_confirmed_apply" if result["drift_clients"] else "no_durable_change",
                    "available": bool(result["drift_clients"]),
                    "requires_repair_id": True,
                },
            },
            "recommendation": recommendation,
        }
    )
    _write_plan(
        Path(repo_root),
        identifier,
        {"schema_version": SCHEMA_VERSION, "repair_id": identifier, "clients": selected, "backup_dir": None, "status": "preview", "created_at": _utc_now()},
    )
    return result


def build_reprobe(
    *,
    repo_root: Path,
    panel: Mapping[str, Any],
    clients: Any = None,
    repair_id: str | None = None,
) -> dict[str, Any]:
    selected = _normalize_clients(clients)
    identifier = repair_id or _new_repair_id()
    adapters, _generator, _selected_adapters = _adapter_snapshot(Path(repo_root), selected)
    result = _base_result(operation="reprobe", repair_id=identifier, clients=selected, panel=panel, adapters=adapters)
    result["ok"] = True
    result["reprobe"] = {"status": "complete", "read_only": True, "writes_live_state": False}
    return result


def execute_reconnect(
    *,
    repo_root: Path,
    panel_before: Mapping[str, Any],
    panel_after: Callable[[], Mapping[str, Any]],
    clients: Any = None,
    repair_id: str | None = None,
    confirm: bool = False,
    apply_adapters: bool = False,
) -> dict[str, Any]:
    selected = _normalize_clients(clients)
    identifier = repair_id or _new_repair_id()
    adapters, generator, selected_adapters = _adapter_snapshot(Path(repo_root), selected)
    result = _base_result(operation="reconnect", repair_id=identifier, clients=selected, panel=panel_before, adapters=adapters)
    drift_clients = list(result["drift_clients"])
    backup_dir: Path | None = None
    manifest_digest: str | None = None
    canary_result: dict[str, Any] | None = None
    apply_result: dict[str, Any] | None = None

    if not confirm:
        action_status = "confirmation_required"
        reason_code = "explicit_confirmation_required"
    elif not _runtime_healthy(panel_before):
        action_status = "blocked"
        reason_code = "runtime_not_healthy"
    else:
        if result["native_session_live"]:
            action_status = "native_session_live"
            reason_code = "native_lease_live"
        elif result["transport_ready"]:
            action_status = "native_probe_required"
            reason_code = "streamable_http_ready_session_idle"
        else:
            action_status = "transport_repair_required"
            reason_code = "canonical_transport_unavailable"
        if apply_adapters and drift_clients:
            drift_set = set(drift_clients)
            apply_adapters_map = {client: adapter for client, adapter in selected_adapters.items() if client in drift_set}
            canary_result = _sanitize_canary(generator.run_canary(apply_adapters_map, repo_root=Path(repo_root)))
            if not canary_result["ok"]:
                action_status = "blocked"
                reason_code = "adapter_canary_failed"
            else:
                try:
                    raw_apply = generator.run_apply(
                        apply_adapters_map,
                        repo_root=Path(repo_root),
                        backup_root=_backup_root(Path(repo_root)),
                    )
                    apply_result = _sanitize_apply(raw_apply)
                    if not apply_result["ok"]:
                        action_status = "blocked"
                        reason_code = "adapter_apply_failed"
                    else:
                        backup_dir = _safe_backup_dir(Path(repo_root), raw_apply.get("backup_dir"))
                        if backup_dir is None:
                            raise McpRepairError("adapter apply returned no rollback backup")
                        manifest_path = backup_dir / "manifest.json"
                        assert_safe_path(manifest_path)
                        manifest_digest = _sha256_file(manifest_path)
                        action_status = "client_reload_required"
                        reason_code = "client_restart_api_unavailable"
                except Exception:  # pragma: no cover - defensive local filesystem boundary
                    action_status = "blocked"
                    reason_code = "adapter_apply_failed"

    _write_plan(
        Path(repo_root),
        identifier,
        {
            "schema_version": SCHEMA_VERSION,
            "repair_id": identifier,
            "clients": selected,
            "backup_dir": str(backup_dir) if backup_dir else None,
            "backup_manifest_sha256": manifest_digest if backup_dir else None,
            "status": action_status,
            "created_at": _utc_now(),
        },
    )
    after = dict(panel_after())
    after_adapters, _after_generator, _after_selected = _adapter_snapshot(Path(repo_root), selected)
    result.update(
        {
            "ok": action_status not in {"blocked"},
            "read_only": not bool(apply_result and apply_result.get("writes_live_state")),
            "writes_live_state": bool(apply_result and apply_result.get("writes_live_state")),
            "action": {
                "status": action_status,
                "reason_code": reason_code,
                "performed": False,
                "auto_repair": False,
                "client_reload_required": action_status == "client_reload_required",
                "native_probe_required": action_status == "native_probe_required",
                "transport_repair_required": action_status == "transport_repair_required",
                "reconnect_scope": "bhm-only",
                "runtime_restart": "not_requested",
            },
            "canary": canary_result,
            "apply": apply_result,
            "reprobe": {
                "status": "complete",
                "read_only": True,
                "writes_live_state": False,
                "panel": after,
                "adapters": after_adapters,
            },
            "rollback": {
                "available": backup_dir is not None,
                "requires_repair_id": True,
                "status": "available" if backup_dir else "no_durable_change",
            },
        }
    )
    return result


def execute_rollback(
    *,
    repo_root: Path,
    repair_id: str,
    panel_after: Callable[[], Mapping[str, Any]],
    confirm: bool = False,
) -> dict[str, Any]:
    selected_plan = _read_plan(Path(repo_root), repair_id)
    if not selected_plan:
        raise McpRepairError("repair plan was not found")
    selected = _normalize_clients(selected_plan.get("clients"))
    backup_dir = _safe_backup_dir(Path(repo_root), selected_plan.get("backup_dir"))
    if backup_dir is None:
        panel = dict(panel_after())
        adapters, _generator, _selected = _adapter_snapshot(Path(repo_root), selected)
        result = _base_result(operation="rollback", repair_id=repair_id, clients=selected, panel=panel, adapters=adapters)
        result.update(
            {
                "ok": True,
                "action": {"status": "nothing_to_rollback", "reason_code": "no_durable_change", "performed": False},
                "rollback": {"available": False, "attempted": False, "status": "no_durable_change"},
                "reprobe": {"status": "complete", "read_only": True, "writes_live_state": False},
            }
        )
        return result
    if not confirm:
        panel = dict(panel_after())
        adapters, _generator, _selected = _adapter_snapshot(Path(repo_root), selected)
        result = _base_result(operation="rollback", repair_id=repair_id, clients=selected, panel=panel, adapters=adapters)
        result.update(
            {
                "ok": True,
                "action": {"status": "confirmation_required", "reason_code": "explicit_confirmation_required", "performed": False},
                "rollback": {"available": True, "attempted": False, "status": "confirmation_required"},
                "reprobe": {"status": "complete", "read_only": True, "writes_live_state": False},
            }
        )
        return result

    expected_manifest_digest = str(selected_plan.get("backup_manifest_sha256") or "").strip().lower()
    if expected_manifest_digest:
        manifest_path = backup_dir / "manifest.json"
        assert_safe_path(manifest_path)
        if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_digest) or _sha256_file(manifest_path) != expected_manifest_digest:
            raise McpRepairError("rollback manifest digest does not match the applied repair plan")
    _validate_backup_scope(Path(repo_root), backup_dir, selected)
    generator, _adapters = _adapter_context(Path(repo_root), selected)
    try:
        raw = generator.run_rollback(backup_dir)
    except Exception as exc:  # pragma: no cover - defensive local filesystem boundary
        raise McpRepairError("adapter rollback failed") from exc
    panel = dict(panel_after())
    adapters, _generator, _selected = _adapter_snapshot(Path(repo_root), selected)
    result = _base_result(operation="rollback", repair_id=repair_id, clients=selected, panel=panel, adapters=adapters)
    rollback = _sanitize_rollback(raw)
    result.update(
        {
            "ok": rollback["ok"],
            "read_only": False,
            "writes_live_state": True,
            "action": {"status": "rolled_back" if rollback["ok"] else "rollback_failed", "reason_code": "exact_byte_rollback", "performed": True},
            "rollback": rollback,
            "reprobe": {"status": "complete", "read_only": True, "writes_live_state": False},
        }
    )
    _write_plan(
        Path(repo_root),
        repair_id,
        {**selected_plan, "status": "rolled_back", "backup_dir": None, "updated_at": _utc_now()},
    )
    return result


__all__ = [
    "McpRepairError",
    "SCHEMA_VERSION",
    "SUPPORTED_CLIENTS",
    "build_repair_preview",
    "build_reprobe",
    "execute_reconnect",
    "execute_rollback",
]
