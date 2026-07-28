"""Single-owner MCP registration contract and read-only drift gate.

The registration layer deliberately does not edit client configuration.  It
normalizes the small set of fields that define BHM ownership (transport and
    URL/auth environment reference), fingerprints them
deterministically, and fails closed when a
client exposes an active alias, duplicate, malformed or drifted BHM
registration.  An explicit ``enabled = false`` TOML override is treated as a
client-side suppression of a plugin-provided alias, so the disabled entry is
not counted as an active registration and its remaining fields are not
required.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from .runtime_endpoints import endpoint_url

DEFAULT_BASE_URL = endpoint_url("bhm_api")
DEFAULT_MCP_URL = endpoint_url("bhm_api", "mcp")
DEFAULT_CANONICAL_SERVER_ID = "bhm"
DEFAULT_ALIASES: tuple[str, ...] = ()
_WINDOWS_ROOT_PATTERN = re.compile(r"(?i)([a-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)")


class RegistrationContractError(ValueError):
    """Raised when a registration document cannot be trusted."""


def resolve_contract_path(path: Path | str, *, repo_root: Path | str | None = None) -> Path:
    """Resolve a registration manifest inside the repository config boundary."""

    root_name = os.path.realpath(os.fspath(repo_root or Path(__file__).resolve().parents[2]))
    raw_path = os.path.expanduser(os.fspath(path))
    resolved_name = os.path.realpath(raw_path if os.path.isabs(raw_path) else os.path.join(root_name, raw_path))
    try:
        contained = os.path.commonpath((root_name, resolved_name)) == root_name
        config_root_name = os.path.join(root_name, "config")
        in_config = os.path.commonpath((config_root_name, resolved_name)) == config_root_name
    except ValueError as exc:
        raise RegistrationContractError("MCP registration manifest must remain under repository config") from exc
    if not contained or not in_config:
        raise RegistrationContractError("MCP registration manifest must remain under repository config")
    resolved = Path(resolved_name)
    if resolved.suffix.casefold() != ".json":
        raise RegistrationContractError("MCP registration manifest must be a JSON file")
    return resolved


@dataclass(frozen=True)
class Registration:
    client: str
    source: str
    server_id: str
    transport: str
    url: str
    command: str
    args: tuple[str, ...]
    base_url: str
    auth_kind: str
    auth_env: str
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "source": self.source,
            "server_id": self.server_id,
            "transport": self.transport,
            "url": self.url,
            "command": self.command,
            "args": list(self.args),
            "base_url": self.base_url,
            "auth_kind": self.auth_kind,
            "auth_env": self.auth_env,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class RegistrationContract:
    schema_version: int
    canonical_server_id: str
    aliases: tuple[str, ...]
    default_base_url: str
    canonical: Mapping[str, Any]
    policy: Mapping[str, Any]

    @property
    def known_ids(self) -> frozenset[str]:
        return frozenset((self.canonical_server_id, *self.aliases))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_base_url(value: str | None, default: str = DEFAULT_BASE_URL) -> str:
    """Normalize an HTTP base URL without retaining secrets or fragments."""

    raw = str(value or default).strip()
    if not raw:
        raw = default
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        raise RegistrationContractError(f"invalid BHM base URL: {raw!r}")
    if parsed.username or parsed.password:
        raise RegistrationContractError("BHM base URL must not contain credentials")
    host = (parsed.hostname or "").lower()
    if not host:
        raise RegistrationContractError("BHM base URL has no host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RegistrationContractError("BHM base URL has an invalid port") from exc
    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if port is None else f"{display_host}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def normalize_mcp_url(value: str | None, default: str = DEFAULT_MCP_URL) -> str:
    normalized = normalize_base_url(value, default)
    parsed = urlsplit(normalized)
    if parsed.scheme != "http" or (parsed.hostname or "").casefold() not in {"127.0.0.1", "localhost", "::1"}:
        raise RegistrationContractError("canonical BHM MCP URL must use loopback HTTP")
    if parsed.path.rstrip("/") != "/mcp":
        raise RegistrationContractError("canonical BHM MCP URL must use the /mcp endpoint")
    return normalized


def _replace_root(value: str, root: Path | None, token: str) -> str:
    if root is None:
        return value
    root_text = ntpath.normpath(str(root)).replace("/", "\\").rstrip("\\")
    candidate = value.replace("/", "\\")
    if candidate.casefold() == root_text.casefold():
        return token
    prefix = root_text + "\\"
    if candidate.casefold().startswith(prefix.casefold()):
        return token + "/" + candidate[len(prefix) :].replace("\\", "/")
    return value


def normalize_argument(
    value: str,
    *,
    repo_root: Path | None = None,
    workspace_root: Path | None = None,
    user_root: Path | None = None,
) -> str:
    """Normalize an argument while preserving command semantics.

    Absolute Windows paths are made portable for fingerprints.  The same
    replacement is also applied inside PowerShell ``-Command`` payloads so a
    generated wrapper cannot evade drift detection merely by changing a root
    spelling.
    """

    result = str(value).strip()
    result = _replace_root(result, repo_root, "<repo>")
    result = _replace_root(result, workspace_root, "<workspace>")
    result = _replace_root(result, user_root, "<user>")
    result = result.replace("\\", "/")
    result = re.sub(r"[ \t]+", " ", result)
    return result


def normalize_command(value: str) -> str:
    command = str(value).strip().replace("\\", "/")
    name = command.rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def registration_identity(
    *,
    command: str = "",
    args: Iterable[str] = (),
    base_url: str | None = None,
    transport: str | None = None,
    url: str | None = None,
    auth_kind: str = "",
    auth_env: str = "",
    default_base_url: str = DEFAULT_BASE_URL,
    default_mcp_url: str = DEFAULT_MCP_URL,
    repo_root: Path | None = None,
    workspace_root: Path | None = None,
    user_root: Path | None = None,
) -> dict[str, Any]:
    resolved_transport = str(transport or "streamable_http").strip()
    if resolved_transport in {"http", "streamable-http"}:
        resolved_transport = "streamable_http"
    if resolved_transport == "streamable_http":
        if str(command or "").strip() or tuple(args):
            raise RegistrationContractError("HTTP MCP identity must not mix url with command/args")
        return {
            "transport": resolved_transport,
            "url": normalize_mcp_url(url, default=default_mcp_url),
            "auth_kind": str(auth_kind or "").strip(),
            "auth_env": str(auth_env or "").strip(),
        }
    raise RegistrationContractError("legacy stdio MCP transport is retired; use canonical Streamable HTTP")


def registration_fingerprint(identity: Mapping[str, Any]) -> str:
    transport = str(identity.get("transport") or "streamable_http")
    if transport != "streamable_http":
        raise RegistrationContractError("only canonical Streamable HTTP registrations are supported")
    return _sha256(
        _canonical_json(
            {
                "transport": transport,
                "url": str(identity.get("url", "")),
                "auth_kind": str(identity.get("auth_kind", "")),
                "auth_env": str(identity.get("auth_env", "")),
            }
        )
    )


def load_contract(path: Path, *, repo_root: Path | None = None) -> RegistrationContract:
    path = resolve_contract_path(path, repo_root=repo_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistrationContractError("cannot read MCP registration contract") from exc
    if not isinstance(payload, dict):
        raise RegistrationContractError("MCP registration contract must be an object")
    canonical_id = str(payload.get("canonical_server_id") or DEFAULT_CANONICAL_SERVER_ID).strip()
    aliases = tuple(str(item).strip() for item in payload.get("aliases", DEFAULT_ALIASES) if str(item).strip())
    if aliases:
        raise RegistrationContractError("MCP registration aliases are retired; use the canonical 'bhm' server id")
    configured_base_url = os.environ.get("BHM_MCP_BASE_URL") or os.environ.get("BHM_BASE_URL")
    default_base_url = normalize_base_url(
        configured_base_url or payload.get("default_base_url"),
        DEFAULT_BASE_URL,
    )
    canonical = payload.get("canonical")
    if not isinstance(canonical, dict):
        raise RegistrationContractError("MCP registration contract has no canonical identity")
    transport = str(canonical.get("transport") or "streamable_http")
    if transport == "streamable_http":
        explicit_url = str(os.environ.get("BHM_MCP_URL") or canonical.get("url") or "").strip()
        if not explicit_url:
            path = str(canonical.get("url_path") or "/mcp")
            if configured_base_url:
                explicit_url = f"{normalize_base_url(configured_base_url)}/{path.lstrip('/')}"
            else:
                explicit_url = endpoint_url(str(canonical.get("url_service") or "bhm_api"), path)
        auth = canonical.get("auth") if isinstance(canonical.get("auth"), dict) else {}
        if auth.get("kind") != "bearer_env" or not str(auth.get("token_env") or "").strip():
            raise RegistrationContractError("canonical streamable HTTP auth must reference a bearer environment variable")
        canonical_identity = registration_identity(
            transport=transport,
            url=explicit_url,
            auth_kind="bearer_env",
            auth_env=str(auth["token_env"]),
            default_mcp_url=endpoint_url("bhm_api", "mcp"),
        )
    else:
        raise RegistrationContractError("canonical MCP transport must be Streamable HTTP")
    policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    return RegistrationContract(
        schema_version=int(payload.get("schema_version", 1)),
        canonical_server_id=canonical_id,
        aliases=aliases,
        default_base_url=default_base_url,
        canonical={**canonical_identity, "fingerprint": registration_fingerprint(canonical_identity)},
        policy=policy,
    )


def _env_value(env: Mapping[str, Any], key: str) -> str | None:
    for name, value in env.items():
        if str(name).casefold() == key.casefold():
            return str(value) if value is not None else None
    return None


def _is_bhm_candidate(
    server_id: str,
    command: str,
    args: Iterable[str],
    env: Mapping[str, Any],
    *,
    url: str = "",
    default_mcp_url: str = DEFAULT_MCP_URL,
) -> bool:
    lowered_id = server_id.casefold()
    if lowered_id in {DEFAULT_CANONICAL_SERVER_ID, *DEFAULT_ALIASES}:
        return True
    if str(url or "").strip():
        try:
            return normalize_mcp_url(url, default_mcp_url) == normalize_mcp_url(default_mcp_url, default_mcp_url)
        except RegistrationContractError:
            return False
    return False


def _is_disabled(spec: Mapping[str, Any]) -> bool:
    """Return whether a client explicitly suppresses this MCP entry."""

    return spec.get("enabled") is False


def _parse_server_map(payload: Mapping[str, Any], *, path: Path) -> Mapping[str, Any]:
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        raise RegistrationContractError(f"{path}: missing JSON mcpServers object")
    return servers


def _http_auth_reference(
    spec: Mapping[str, Any],
    *,
    source: Path,
    server_id: str,
    named_bhm_surface: bool,
) -> tuple[str, str]:
    bearer_env = str(spec.get("bearer_token_env_var") or "").strip()
    headers = spec.get("headers") if isinstance(spec.get("headers"), dict) else {}
    authorization = str(headers.get("Authorization") or headers.get("authorization") or "").strip()
    if authorization:
        match = re.fullmatch(r"Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}", authorization)
        if not match:
            if named_bhm_surface:
                raise RegistrationContractError(
                    f"{source}: server {server_id!r} contains a literal or malformed Authorization header"
                )
            return "", ""
        header_env = match.group(1)
        if bearer_env and bearer_env != header_env:
            raise RegistrationContractError(f"{source}: server {server_id!r} has conflicting bearer references")
        bearer_env = header_env
    return ("bearer_env", bearer_env) if bearer_env else ("", "")


def _registration_from_spec(
    *,
    client: str,
    source: Path,
    server_id: str,
    spec: Mapping[str, Any],
    repo_root: Path | None,
    workspace_root: Path | None,
    user_root: Path | None,
    default_base_url: str,
    default_mcp_url: str,
) -> Registration | None:
    if _is_disabled(spec):
        return None
    url = str(spec.get("url") or "").strip()
    command = spec.get("command")
    args = spec.get("args", [])
    env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
    named_bhm_surface = server_id.casefold() in {DEFAULT_CANONICAL_SERVER_ID, *DEFAULT_ALIASES}
    if url:
        if command is not None or "args" in spec or env:
            if named_bhm_surface:
                raise RegistrationContractError(f"{source}: server {server_id!r} mixes url with command/args/env")
            return None
        if not _is_bhm_candidate(
            server_id,
            "",
            (),
            {},
            url=url,
            default_mcp_url=default_mcp_url,
        ):
            return None
        auth_kind, auth_env = _http_auth_reference(
            spec,
            source=source,
            server_id=server_id,
            named_bhm_surface=named_bhm_surface,
        )
        identity = registration_identity(
            transport="streamable_http",
            url=url,
            auth_kind=auth_kind,
            auth_env=auth_env,
            default_mcp_url=default_mcp_url,
        )
        return Registration(
            client=client,
            source=str(source),
            server_id=server_id,
            transport="streamable_http",
            url=str(identity["url"]),
            command="",
            args=(),
            base_url="",
            auth_kind=auth_kind,
            auth_env=auth_env,
            fingerprint=registration_fingerprint(identity),
        )
    if not isinstance(command, str):
        if named_bhm_surface:
            raise RegistrationContractError(f"{source}: server {server_id!r} has malformed command/args")
        return None
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        if named_bhm_surface:
            raise RegistrationContractError(f"{source}: server {server_id!r} has malformed command/args")
        return None
    if _is_bhm_candidate(server_id, command, args, env, default_mcp_url=default_mcp_url):
        raise RegistrationContractError(f"{source}: server {server_id!r} uses retired stdio MCP transport")
    return None


def load_json_registrations(
    path: Path,
    *,
    client: str,
    repo_root: Path | None = None,
    workspace_root: Path | None = None,
    user_root: Path | None = None,
    default_base_url: str = DEFAULT_BASE_URL,
    default_mcp_url: str = DEFAULT_MCP_URL,
) -> list[Registration]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistrationContractError(f"cannot read MCP JSON surface: {path}") from exc
    if not isinstance(payload, dict):
        raise RegistrationContractError(f"{path}: JSON root must be an object")
    servers = _parse_server_map(payload, path=path)
    result: list[Registration] = []
    for server_id, spec in servers.items():
        if not isinstance(server_id, str) or not isinstance(spec, dict):
            raise RegistrationContractError(f"{path}: malformed server entry")
        registration = _registration_from_spec(
            client=client,
            source=path,
            server_id=server_id,
            spec=spec,
            repo_root=repo_root,
            workspace_root=workspace_root,
            user_root=user_root,
            default_base_url=default_base_url,
            default_mcp_url=default_mcp_url,
        )
        if registration is not None:
            result.append(registration)
    return result


def load_toml_registrations(
    path: Path,
    *,
    client: str,
    repo_root: Path | None = None,
    workspace_root: Path | None = None,
    user_root: Path | None = None,
    default_base_url: str = DEFAULT_BASE_URL,
    default_mcp_url: str = DEFAULT_MCP_URL,
) -> list[Registration]:
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistrationContractError(f"cannot read MCP TOML surface: {path}") from exc
    servers = payload.get("mcp_servers")
    if not isinstance(servers, dict):
        raise RegistrationContractError(f"{path}: missing mcp_servers table")
    result: list[Registration] = []
    for server_id, spec in servers.items():
        if not isinstance(server_id, str) or not isinstance(spec, dict):
            raise RegistrationContractError(f"{path}: malformed MCP server table")
        registration = _registration_from_spec(
            client=client,
            source=path,
            server_id=server_id,
            spec=spec,
            repo_root=repo_root,
            workspace_root=workspace_root,
            user_root=user_root,
            default_base_url=default_base_url,
            default_mcp_url=default_mcp_url,
        )
        if registration is not None:
            result.append(registration)
    return result


def load_registrations(
    path: Path,
    *,
    client: str = "codex",
    repo_root: Path | None = None,
    workspace_root: Path | None = None,
    user_root: Path | None = None,
    default_base_url: str = DEFAULT_BASE_URL,
    default_mcp_url: str = DEFAULT_MCP_URL,
) -> list[Registration]:
    if path.suffix.casefold() == ".toml":
        return load_toml_registrations(
            path,
            client=client,
            repo_root=repo_root,
            workspace_root=workspace_root,
            user_root=user_root,
            default_base_url=default_base_url,
            default_mcp_url=default_mcp_url,
        )
    return load_json_registrations(
        path,
        client=client,
        repo_root=repo_root,
        workspace_root=workspace_root,
        user_root=user_root,
        default_base_url=default_base_url,
        default_mcp_url=default_mcp_url,
    )


def evaluate_registrations(
    contract: RegistrationContract,
    registrations: Iterable[Registration],
) -> dict[str, Any]:
    items = sorted(
        (registration for registration in registrations),
        key=lambda item: (item.client, item.server_id, item.source),
    )
    issues: list[dict[str, Any]] = []
    by_client: dict[str, list[Registration]] = {}
    for registration in items:
        by_client.setdefault(registration.client, []).append(registration)

    expected_fingerprint = str(contract.canonical["fingerprint"])
    for client, entries in sorted(by_client.items()):
        canonical = [item for item in entries if item.server_id == contract.canonical_server_id]
        aliases = [item for item in entries if item.server_id in contract.aliases]
        unknown = [item for item in entries if item.server_id not in contract.known_ids]
        if len(canonical) != 1:
            issues.append(
                {
                    "code": "canonical_registration_count",
                    "client": client,
                    "count": len(canonical),
                    "expected": 1,
                }
            )
        if aliases:
            issues.append(
                {
                    "code": "alias_registration",
                    "client": client,
                    "server_ids": sorted({item.server_id for item in aliases}),
                    "sources": sorted(item.source for item in aliases),
                }
            )
        if unknown:
            issues.append(
                {
                    "code": "unrecognized_bhm_surface",
                    "client": client,
                    "server_ids": sorted({item.server_id for item in unknown}),
                    "sources": sorted(item.source for item in unknown),
                }
            )
        counts = Counter(item.fingerprint for item in entries)
        duplicates = sorted(fingerprint for fingerprint, count in counts.items() if count > 1)
        if duplicates:
            issues.append(
                {
                    "code": "duplicate_fingerprint",
                    "client": client,
                    "fingerprints": duplicates,
                }
            )
        if len(canonical) == 1 and canonical[0].fingerprint != expected_fingerprint:
            issues.append(
                {
                    "code": "canonical_fingerprint_drift",
                    "client": client,
                    "source": canonical[0].source,
                    "actual": canonical[0].fingerprint,
                    "expected": expected_fingerprint,
                }
            )

    inventory = [item.as_dict() for item in items]
    return {
        "ok": not issues,
        "fail_closed": bool(issues),
        "schema_version": contract.schema_version,
        "canonical_server_id": contract.canonical_server_id,
        "expected_fingerprint": expected_fingerprint,
        "registration_count": len(items),
        "registrations": inventory,
        "issues": issues,
        "inventory_digest": _sha256(_canonical_json(inventory)),
        "writes_live_state": False,
    }


def canonical_fixture(contract: RegistrationContract, *, source: str = "fixture") -> Registration:
    identity = contract.canonical
    return Registration(
        client="codex",
        source=source,
        server_id=contract.canonical_server_id,
        transport=str(identity.get("transport") or "streamable_http"),
        url=str(identity.get("url") or ""),
        command=str(identity.get("command") or ""),
        args=tuple(str(item) for item in identity.get("args", ())),
        base_url=str(identity.get("base_url") or ""),
        auth_kind=str(identity.get("auth_kind") or ""),
        auth_env=str(identity.get("auth_env") or ""),
        fingerprint=str(identity["fingerprint"]),
    )
