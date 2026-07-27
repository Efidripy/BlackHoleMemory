"""Generate and verify BHM MCP client adapters from one manifest.

The manifest owns shared command/base-url/env semantics.  Codex, Claude and
the repository plugin keep only the constraints that their host format needs:
TOML versus JSON, server id, target and reload action.  The default operation
is read-only drift checking; applying changes requires an explicit canary and
always creates an atomic, timestamped backup that can be rolled back.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from blackholememory.runtime_endpoints import endpoint_url


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "config" / "mcp-registration.json"
BACKUP_ROOT = REPO_ROOT.parent.parent / "workspace" / "runtime" / "logs" / "mcp-adapters" / "backups"
SCHEMA = "bhm.mcp.adapter-generation.v2"
TOKEN_RE = re.compile(r"<(?P<name>repo|user|workspace)>", re.IGNORECASE)


class AdapterContractError(ValueError):
    """Raised for malformed manifests, surfaces or unsafe mutations."""


@dataclass(frozen=True)
class Adapter:
    client: str
    format: str
    server_id: str
    target: Path
    managed_scope: str
    reload_action: str
    transport: str
    url: str
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]
    extra: Mapping[str, Any]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _now_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%fZ")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_url(value: Any, default: str) -> str:
    raw = str(value or default).strip()
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc or parsed.username or parsed.password:
        raise AdapterContractError(f"invalid adapter base URL: {raw!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise AdapterContractError("adapter base URL has no host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise AdapterContractError("adapter base URL has invalid port") from exc
    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if port is None else f"{display_host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))


def _normalize_mcp_url(value: Any, default: str) -> str:
    normalized = _normalize_url(value, default)
    parsed = urlsplit(normalized)
    if parsed.scheme != "http" or (parsed.hostname or "").casefold() not in {"127.0.0.1", "localhost", "::1"}:
        raise AdapterContractError("canonical BHM MCP URL must use loopback HTTP")
    if parsed.path.rstrip("/") != "/mcp":
        raise AdapterContractError("canonical BHM MCP URL must use the /mcp endpoint")
    return normalized


def _resolve_token(value: str, *, repo_root: Path, user_root: Path, workspace_root: Path) -> str:
    roots = {"repo": repo_root, "user": user_root, "workspace": workspace_root}

    def replace(match: re.Match[str]) -> str:
        return str(roots[match.group("name").lower()])

    return TOKEN_RE.sub(replace, str(value))


def _normalize_path_text(value: str, *, repo_root: Path, user_root: Path, workspace_root: Path) -> str:
    result = str(value).replace("/", "\\")
    for root, token in ((repo_root, "<repo>"), (user_root, "<user>"), (workspace_root, "<workspace>")):
        root_text = str(root).replace("/", "\\").rstrip("\\")
        if result.casefold() == root_text.casefold():
            result = token
        elif result.casefold().startswith((root_text + "\\").casefold()):
            result = token + "\\" + result[len(root_text) + 1 :]
    result = result.replace("\\", "/")
    return re.sub(r"[ \t]+", " ", result).strip()


def _normalize_command(value: Any) -> str:
    name = str(value).strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _normalize_identity(
    spec: Mapping[str, Any],
    *,
    repo_root: Path,
    user_root: Path,
    workspace_root: Path,
    default_url: str,
) -> dict[str, Any]:
    url = str(spec.get("url") or "").strip()
    command = spec.get("command")
    env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
    if url:
        if command is not None or "args" in spec or env:
            raise AdapterContractError("HTTP adapter entry must not mix url with command/args/env")
        bearer_env = str(spec.get("bearer_token_env_var") or "").strip()
        headers = spec.get("headers") if isinstance(spec.get("headers"), dict) else {}
        authorization = str(headers.get("Authorization") or headers.get("authorization") or "").strip()
        if authorization:
            match = re.fullmatch(r"Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}", authorization)
            if not match:
                raise AdapterContractError("HTTP adapter Authorization must reference a bearer environment variable")
            header_env = match.group(1)
            if bearer_env and bearer_env != header_env:
                raise AdapterContractError("HTTP adapter bearer environment references disagree")
            bearer_env = header_env
        if not bearer_env:
            raise AdapterContractError("HTTP adapter bearer environment reference is missing")
        return {
            "transport": "streamable_http",
            "url": _normalize_mcp_url(url, default_url),
            "auth_kind": "bearer_env",
            "auth_env": bearer_env,
        }
    raise AdapterContractError("legacy stdio MCP adapter is retired; use canonical Streamable HTTP")


def _contract(manifest_path: Path, repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterContractError(f"cannot read adapter manifest: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise AdapterContractError("adapter manifest must be an object")
    adapter_contract = payload.get("adapter_contract")
    if not isinstance(adapter_contract, dict) or adapter_contract.get("schema_version") != "bhm.mcp.adapter-contract.v3":
        raise AdapterContractError("manifest has no bhm.mcp.adapter-contract.v3 adapter_contract")
    common = adapter_contract.get("common")
    clients = adapter_contract.get("clients")
    policy = adapter_contract.get("policy")
    if not isinstance(common, dict) or not isinstance(clients, dict) or not isinstance(policy, dict):
        raise AdapterContractError("adapter_contract common/clients/policy are malformed")
    required_policy = ("atomic_backup", "canary_required_before_apply", "rollback_required", "client_specific_constraints_explicit")
    if any(policy.get(key) is not True for key in required_policy):
        raise AdapterContractError("adapter contract safety policy is not fully enabled")
    for key in ("server_id", "transport", "url", "url_service", "url_path"):
        if key not in common:
            raise AdapterContractError(f"adapter common field is missing: {key}")
    if common.get("transport") != "streamable_http":
        raise AdapterContractError("only canonical Streamable HTTP adapters are supported")
    auth = common.get("auth")
    if not isinstance(auth, dict) or auth.get("kind") != "bearer_env":
        raise AdapterContractError("streamable HTTP adapter common auth must be bearer_env")
    token_env = str(auth.get("token_env") or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        raise AdapterContractError("streamable HTTP adapter token_env is malformed")
    for client in ("codex", "claude"):
        spec = clients.get(client)
        if not isinstance(spec, dict):
            raise AdapterContractError(f"adapter client is missing: {client}")
        for key in ("format", "server_id", "target", "managed_scope", "reload_action"):
            if not isinstance(spec.get(key), str) or not spec[key].strip():
                raise AdapterContractError(f"adapter {client} field is missing or empty: {key}")
        if spec["format"] not in {"json", "toml"}:
            raise AdapterContractError(f"adapter {client} has unsupported format: {spec['format']}")
    return payload, adapter_contract


def _adapters(manifest: Mapping[str, Any], adapter_contract: Mapping[str, Any], repo_root: Path) -> dict[str, Adapter]:
    common = adapter_contract["common"]
    workspace_root = repo_root.parent.parent
    user_root = Path(os.environ.get("USERPROFILE") or Path.home())
    result: dict[str, Adapter] = {}
    for client, raw in adapter_contract["clients"].items():
        transport = str(raw.get("transport", common["transport"]))
        if transport != "streamable_http":
            raise AdapterContractError(f"adapter {client} uses retired transport: {transport}")
        url = ""
        command = ""
        args: tuple[str, ...] = ()
        env: dict[str, str] = {}
        if transport == "streamable_http":
            url = str(raw.get("url", common.get("url", ""))).strip()
            if not url:
                service = str(raw.get("url_service", common.get("url_service", "bhm_api")))
                path = str(raw.get("url_path", common.get("url_path", "/mcp")))
                url = endpoint_url(service, path)
            url = _normalize_mcp_url(url, endpoint_url("bhm_api", "mcp"))
        target = Path(_resolve_token(str(raw["target"]), repo_root=repo_root, user_root=user_root, workspace_root=workspace_root))
        extra = copy.deepcopy(raw.get("extra")) if isinstance(raw.get("extra"), dict) else {}
        if transport == "streamable_http":
            auth = common["auth"]
            token_env = str(auth["token_env"])
            if client == "codex":
                extra["bearer_token_env_var"] = token_env
            elif client == "claude":
                headers = copy.deepcopy(extra.get("headers")) if isinstance(extra.get("headers"), dict) else {}
                headers["Authorization"] = f"Bearer ${{{token_env}}}"
                extra["headers"] = headers
        result[client] = Adapter(
            client=client,
            format=str(raw["format"]),
            server_id=str(raw["server_id"]),
            target=target,
            managed_scope=str(raw["managed_scope"]),
            reload_action=str(raw["reload_action"]),
            transport=transport,
            url=url,
            command=command,
            args=args,
            env=env,
            extra=extra,
        )
    return result


def _entry(adapter: Adapter, *, repo_root: Path) -> dict[str, Any]:
    if adapter.transport == "streamable_http":
        if adapter.command or adapter.args or adapter.env:
            raise AdapterContractError("HTTP adapter cannot contain stdio command/args/env")
        return {"url": adapter.url, **copy.deepcopy(dict(adapter.extra))}
    raise AdapterContractError("legacy stdio MCP adapter is retired")


def _render_json(path: Path, adapter: Adapter, *, repo_root: Path) -> str:
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterContractError(f"{path}: malformed JSON surface") from exc
    if not isinstance(current, dict):
        raise AdapterContractError(f"{path}: JSON root must be an object")
    servers = current.get("mcpServers")
    if servers is None:
        servers = {}
        current["mcpServers"] = servers
    if not isinstance(servers, dict):
        raise AdapterContractError(f"{path}: mcpServers must be an object")
    servers[adapter.server_id] = _entry(adapter, repo_root=repo_root)
    return json.dumps(current, ensure_ascii=False, indent=2) + "\n"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "[" + ", ".join(_toml_string(item) for item in value) + "]"
    raise AdapterContractError(f"unsupported TOML adapter value: {type(value).__name__}")


def _render_toml_block(adapter: Adapter, *, repo_root: Path) -> str:
    entry = _entry(adapter, repo_root=repo_root)
    lines = [f"[mcp_servers.{adapter.server_id}]"]
    env = entry.pop("env", {})
    for key, value in entry.items():
        lines.append(f"{key} = {_toml_value(value)}")
    if env:
        lines.append(f"[mcp_servers.{adapter.server_id}.env]")
        for key, value in env.items():
            lines.append(f"{key} = {_toml_string(str(value))}")
    return "\n".join(lines) + "\n"


def _render_toml(path: Path, adapter: Adapter, *, repo_root: Path) -> str:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        tomllib.loads(original or "")
    except tomllib.TOMLDecodeError as exc:
        raise AdapterContractError(f"{path}: malformed TOML surface") from exc
    lines = original.splitlines(keepends=True)
    prefix = f"[mcp_servers.{adapter.server_id}"
    start: int | None = None
    end: int | None = None
    for index, line in enumerate(lines):
        header = line.strip()
        if header == f"[mcp_servers.{adapter.server_id}]" or header.startswith(prefix + "."):
            if start is None:
                start = index
            continue
        if start is not None and header.startswith("["):
            end = index
            break
    block = _render_toml_block(adapter, repo_root=repo_root)
    if start is None:
        base = original.rstrip("\r\n")
        return (base + "\n\n" if base else "") + block
    if end is None:
        end = len(lines)
    newline = "\r\n" if "\r\n" in original else "\n"
    block = block.replace("\n", newline)
    return "".join(lines[:start]) + block + "".join(lines[end:])


def _read_entry(path: Path, adapter: Adapter) -> dict[str, Any]:
    if not path.exists():
        raise AdapterContractError(f"{path}: target is missing")
    try:
        if adapter.format == "json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            entry = ((payload.get("mcpServers") or {}).get(adapter.server_id)) if isinstance(payload, dict) else None
        else:
            with path.open("rb") as handle:
                payload = tomllib.load(handle)
            entry = ((payload.get("mcp_servers") or {}).get(adapter.server_id)) if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise AdapterContractError(f"{path}: cannot parse target") from exc
    if not isinstance(entry, dict):
        raise AdapterContractError(f"{path}: managed server {adapter.server_id!r} is missing")
    return entry


def _check_adapter(adapter: Adapter, *, repo_root: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "client": adapter.client,
        "target": str(adapter.target),
        "format": adapter.format,
        "server_id": adapter.server_id,
        "managed_scope": adapter.managed_scope,
        "reload_action": adapter.reload_action,
        "exists": adapter.target.exists(),
        "sha256": _sha256_bytes(adapter.target.read_bytes()) if adapter.target.exists() else None,
        "ok": False,
        "issues": [],
    }
    if not adapter.target.exists():
        record["issues"] = ["target_missing"]
        return record
    try:
        actual = _read_entry(adapter.target, adapter)
        expected = _entry(adapter, repo_root=repo_root)
        default_url = adapter.url or str(adapter.env.get("BHM_MCP_BASE_URL", endpoint_url("bhm_api")))
        actual_identity = _normalize_identity(
            actual,
            repo_root=repo_root,
            user_root=Path(os.environ.get("USERPROFILE") or Path.home()),
            workspace_root=repo_root.parent.parent,
            default_url=default_url,
        )
        expected_identity = _normalize_identity(
            expected,
            repo_root=repo_root,
            user_root=Path(os.environ.get("USERPROFILE") or Path.home()),
            workspace_root=repo_root.parent.parent,
            default_url=default_url,
        )
        issues: list[str] = []
        if actual_identity != expected_identity:
            issues.append("identity_drift")
        for key, value in adapter.extra.items():
            if actual.get(key) != value:
                issues.append(f"extra_drift:{key}")
        record["actual_identity"] = actual_identity
        record["expected_identity"] = expected_identity
        record["issues"] = issues
        record["ok"] = not issues
    except AdapterContractError as exc:
        record["issues"] = [str(exc)]
    return record


def _atomic_write(path: Path, content: str) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"))


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(raw_temp)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _backup_target(path: Path, backup_dir: Path, client: str) -> dict[str, Any]:
    backup_path = backup_dir / f"{client}{path.suffix}.bak"
    existed = path.exists()
    record: dict[str, Any] = {
        "client": client,
        "target": str(path),
        "backup": str(backup_path),
        "existed": existed,
        "sha256_before": _sha256_bytes(path.read_bytes()) if existed else None,
    }
    if existed:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, backup_path)
    return record


def _rollback_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for record in records:
        target = Path(str(record["target"]))
        existed = bool(record.get("existed"))
        if existed:
            backup = Path(str(record["backup"]))
            _atomic_write_bytes(target, backup.read_bytes())
        else:
            target.unlink(missing_ok=True)
        restored_hash = _sha256_bytes(target.read_bytes()) if target.exists() else None
        results.append(
            {
                "client": record.get("client"),
                "target": str(target),
                "restored": restored_hash == record.get("sha256_before"),
                "sha256_after": restored_hash,
            }
        )
    return results


def _write_backup_manifest(backup_dir: Path, records: list[dict[str, Any]]) -> None:
    _atomic_write(backup_dir / "manifest.json", json.dumps({"schema": SCHEMA, "records": records}, indent=2) + "\n")


def _apply_one(adapter: Adapter, path: Path, *, repo_root: Path, backup_dir: Path, records: list[dict[str, Any]]) -> None:
    record = _backup_target(path, backup_dir, adapter.client)
    records.append(record)
    if adapter.format == "json":
        content = _render_json(path, adapter, repo_root=repo_root)
    else:
        content = _render_toml(path, adapter, repo_root=repo_root)
    _atomic_write(path, content)


def run_check(adapters: Mapping[str, Adapter], *, repo_root: Path) -> dict[str, Any]:
    records = [_check_adapter(adapters[name], repo_root=repo_root) for name in sorted(adapters)]
    return {
        "schema": SCHEMA,
        "mode": "check",
        "ok": all(bool(record["ok"]) for record in records),
        "clients": records,
        "writes_live_state": False,
    }


def run_canary(adapters: Mapping[str, Adapter], *, repo_root: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    rollback: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="bhm-mcp-adapter-canary-") as temp:
        root = Path(temp)
        backup_dir = root / "backups"
        for name in sorted(adapters):
            adapter = adapters[name]
            fixture = root / f"{name}{adapter.target.suffix}"
            if adapter.target.exists():
                shutil.copyfile(adapter.target, fixture)
            elif adapter.format == "json":
                fixture.write_text('{"mcpServers": {}}\n', encoding="utf-8")
            else:
                fixture.write_text("", encoding="utf-8")
            before = _sha256_bytes(fixture.read_bytes())
            _apply_one(adapter, fixture, repo_root=repo_root, backup_dir=backup_dir, records=records)
            canary_adapter = Adapter(**{**adapter.__dict__, "target": fixture})
            check = _check_adapter(canary_adapter, repo_root=repo_root)
            check["sha256_before"] = before
            check["sha256_generated"] = _sha256_bytes(fixture.read_bytes())
            checks.append(check)
        rollback = _rollback_records(reversed(records))
        restored = all(item["restored"] for item in rollback)
    return {
        "schema": SCHEMA,
        "mode": "canary",
        "ok": all(bool(item["ok"]) for item in checks) and restored,
        "clients": checks,
        "backup": {"atomic": True, "records": len(records)},
        "rollback": {"attempted": True, "ok": restored, "records": rollback},
        "writes_live_state": False,
    }


def run_apply(adapters: Mapping[str, Adapter], *, repo_root: Path, backup_root: Path) -> dict[str, Any]:
    backup_dir = backup_root / _now_id()
    backup_dir.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, Any]] = []
    try:
        for name in sorted(adapters):
            _apply_one(adapters[name], adapters[name].target, repo_root=repo_root, backup_dir=backup_dir, records=records)
        _write_backup_manifest(backup_dir, records)
    except Exception:
        if records:
            _rollback_records(reversed(records))
        raise
    result = run_check(adapters, repo_root=repo_root)
    result.update({"mode": "apply", "backup_dir": str(backup_dir), "writes_live_state": True})
    result["backup"] = {"atomic": True, "records": records}
    return result


def run_rollback(backup_dir: Path) -> dict[str, Any]:
    manifest_path = backup_dir / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterContractError(f"rollback manifest is unreadable: {manifest_path}") from exc
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise AdapterContractError("rollback manifest has no records")
    result = _rollback_records(reversed(records))
    return {
        "schema": SCHEMA,
        "mode": "rollback",
        "ok": all(item["restored"] for item in result),
        "backup_dir": str(backup_dir),
        "rollback": {"attempted": True, "ok": all(item["restored"] for item in result), "records": result},
        "writes_live_state": True,
    }


def _latest_backup(root: Path) -> Path:
    candidates = [path for path in root.iterdir() if path.is_dir()]
    if not candidates:
        raise AdapterContractError(f"no adapter backups found under {root}")
    return sorted(candidates)[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--backup-root", type=Path, default=BACKUP_ROOT)
    parser.add_argument("--client", choices=("all", "codex", "claude"), default="all")
    parser.add_argument("--check", action="store_true", help="read-only drift check")
    parser.add_argument("--canary", action="store_true", help="apply to temporary fixtures and roll back")
    parser.add_argument("--apply", action="store_true", help="apply after an explicit canary")
    parser.add_argument("--rollback", action="store_true", help="restore a backup directory")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result: dict[str, Any]
    try:
        if args.rollback:
            backup_dir = args.backup_dir or _latest_backup(args.backup_root)
            result = run_rollback(backup_dir)
        else:
            manifest, adapter_contract = _contract(args.manifest, args.repo_root)
            adapters = _adapters(manifest, adapter_contract, args.repo_root)
            if args.client != "all":
                adapters = {args.client: adapters[args.client]}
            if args.apply and not args.canary:
                raise AdapterContractError("apply is fail-closed: run --canary --apply together")
            canary_result = run_canary(adapters, repo_root=args.repo_root) if args.canary else None
            if canary_result is not None and not canary_result["ok"]:
                result = canary_result
            elif args.apply:
                result = run_apply(adapters, repo_root=args.repo_root, backup_root=args.backup_root)
                result["canary"] = canary_result
            else:
                result = canary_result or run_check(adapters, repo_root=args.repo_root)
            result["manifest"] = str(args.manifest)
            result["shared_semantics"] = {
                "server_id": adapter_contract["common"]["server_id"],
                "transport": adapter_contract["common"]["transport"],
                "url": adapter_contract["common"]["url"],
                "url_service": adapter_contract["common"]["url_service"],
                "url_path": adapter_contract["common"]["url_path"],
            }
            result["client_constraints"] = {
                name: {
                    "server_id": adapter.server_id,
                    "format": adapter.format,
                    "target": str(adapter.target),
                    "managed_scope": adapter.managed_scope,
                    "reload_action": adapter.reload_action,
                }
                for name, adapter in sorted(adapters.items())
            }
    except (AdapterContractError, OSError, KeyError) as exc:
        result = {"schema": SCHEMA, "ok": False, "fail_closed": True, "error": str(exc), "writes_live_state": False}
    output = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(output)
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
