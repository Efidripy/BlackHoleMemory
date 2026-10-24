"""Quarantined external-source registry for BHM integration work.

External repositories and web references are untrusted evidence.  This module
may acquire and inventory them under ``.src`` but never imports or executes
their code and never writes to the BHM runtime stores.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .filesystem_boundaries import replace_bytes_safely
from .resource_limits import (
    PROCESS_EXECUTION_SOURCE_REGISTRY_GIT_TIMEOUT_SECONDS,
    SOURCE_REGISTRY_WEB_TIMEOUT_SECONDS,
)

REGISTRY_SCHEMA_V1 = "bhm.source-registry.v1"
REGISTRY_SCHEMA = "bhm.source-registry.v2"
MANIFEST_SCHEMA_V1 = "bhm.source-manifest.v1"
MANIFEST_SCHEMA = "bhm.source-manifest.v2"
SUPPORTED_REGISTRY_SCHEMAS = {REGISTRY_SCHEMA_V1, REGISTRY_SCHEMA}
SUPPORTED_MANIFEST_SCHEMAS = {MANIFEST_SCHEMA_V1, MANIFEST_SCHEMA}
PERMISSION_STATUS_VALUES = {
    "not-mapped",
    "written-permission",
    "scope-limited",
    "denied",
    "expired",
}
TRANSFER_MODE_VALUES = {
    "clean-room",
    "direct-transfer-scoped",
    "contract-only",
    "rejected",
}
PERMISSION_FIELDS = (
    "permission_status",
    "permission_evidence_ref",
    "rightsholder",
    "covered_scope",
    "covered_files",
    "covered_capabilities",
    "third_party_exclusions",
    "permission_checked_at",
)

LICENSE_NAMES = {
    "license",
    "license.md",
    "license.txt",
    "copying",
    "copying.md",
    "copying.txt",
    "notice",
    "notice.md",
    "notice.txt",
}
DEPENDENCY_NAMES = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "cargo.toml",
    "cargo.lock",
    "go.mod",
    "go.sum",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gradle.lockfile",
}
SECURITY_NAMES = {
    "security.md",
    "security.txt",
    "dependabot.yml",
    "dependabot.yaml",
}
RISKY_BASENAMES = {
    ".env",
    "id_rsa",
    "id_dsa",
    "id_ed25519",
}
RISKY_SUFFIXES = {".pem", ".p12", ".pfx", ".key", ".kdbx", ".sqlite", ".sqlite3"}
PRIVATE_KEY_BLOCK = re.compile(
    rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    rb"\s+[A-Za-z0-9+/=\r\n]{160,}\s+"
    rb"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
)
CREDENTIAL_PATTERNS = {
    "aws-access-key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "github-classic-token": re.compile(rb"ghp_[A-Za-z0-9]{36,}"),
    "github-fine-grained-token": re.compile(rb"github_pat_[A-Za-z0-9_]{40,}"),
    "openai-style-token": re.compile(rb"sk-[A-Za-z0-9]{20,}"),
}
MAX_SECRET_SCAN_BYTES = 4 * 1024 * 1024
MAX_WEB_REDIRECTS = 3
MAX_WEB_RESPONSE_BYTES = 16 * 1024 * 1024
LICENSE_SIGNATURES = {
    "Apache-2.0": "Apache License\n                           Version 2.0",
    "AGPL-3.0": "GNU AFFERO GENERAL PUBLIC LICENSE",
    "Elastic-2.0": "Elastic License 2.0",
    "MIT-like": "Permission is hereby granted, free of charge",
}


class SourceRegistryError(RuntimeError):
    """Raised when a source definition or quarantine state is invalid."""


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    )


def _assert_public_dns(hostname: str, port: int | None) -> None:
    try:
        infos = socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceRegistryError("web source hostname could not be resolved") from exc
    addresses = {str(info[4][0]) for info in infos if info and len(info) > 4 and info[4]}
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise SourceRegistryError("web source hostname resolves to a private or local address")


def _validate_web_source_url(value: str, *, resolve_dns: bool = False) -> str:
    """Validate an operator-reviewed public HTTPS source URL."""

    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() != "https":
        raise SourceRegistryError("web source URL must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise SourceRegistryError("web source URL must have a public host without credentials or fragment")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not _is_public_address(str(address)):
        raise SourceRegistryError("web source URL must not target a private or local address")
    if resolve_dns and address is None:
        try:
            port = parsed.port
        except ValueError as exc:
            raise SourceRegistryError("web source URL has an invalid port") from exc
        _assert_public_dns(parsed.hostname, port)
    return raw


_GIT_URL_SCHEMES = frozenset({"file", "git", "git+ssh", "https", "ssh"})
_GIT_SCP_URL_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+$")


def _validate_git_source_url(value: str) -> str:
    """Validate a registry Git remote before placing it in Git argv."""

    raw = str(value or "").strip()
    if not raw or raw.startswith("-") or any(ord(char) < 0x20 or char.isspace() for char in raw):
        raise SourceRegistryError("git source URL must be a non-empty non-option value")
    parsed = urlsplit(raw)
    if not parsed.scheme:
        if not _GIT_SCP_URL_RE.fullmatch(raw):
            raise SourceRegistryError("git source URL must use an allowlisted URL or scp-style form")
        return raw
    scheme = parsed.scheme.casefold()
    if scheme not in _GIT_URL_SCHEMES:
        raise SourceRegistryError(f"git source URL scheme is not allowed: {parsed.scheme}")
    if parsed.fragment or parsed.query:
        raise SourceRegistryError("git source URL must not contain query or fragment material")
    if parsed.password or (parsed.username and not (scheme in {"ssh", "git+ssh"} and parsed.username == "git")):
        raise SourceRegistryError("git source URL must not contain embedded credentials")
    if scheme != "file" and not parsed.hostname:
        raise SourceRegistryError("git source URL must include a host")
    return raw


class _ExternalRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Permit a small, explicitly validated redirect chain for web sources."""

    def __init__(self) -> None:
        super().__init__()
        self._redirect_count = 0

    def redirect_request(self, req, fp, code, msg, headers, new):  # type: ignore[override]
        if self._redirect_count >= MAX_WEB_REDIRECTS:
            raise SourceRegistryError("web source redirect limit exceeded")
        target = super().redirect_request(req, fp, code, msg, headers, new)
        if target is None:
            return None
        _validate_web_source_url(target.full_url, resolve_dns=True)
        self._redirect_count += 1
        return target


def _open_web_source(url: str, *, timeout: float):
    bounded_timeout = bounded_source_registry_web_timeout(timeout)
    validated_url = _validate_web_source_url(url, resolve_dns=True)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _ExternalRedirectHandler(),
    )
    response = opener.open(
        urllib.request.Request(validated_url, headers={"User-Agent": "BlackHoleMemory-source-passport/1.0"}),
        timeout=bounded_timeout,
    )
    socket_obj = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if socket_obj is not None:
        try:
            peer = socket_obj.getpeername()
            peer_address = str(peer[0] if isinstance(peer, tuple) else peer)
        except OSError as exc:
            raise SourceRegistryError("web source peer address could not be inspected") from exc
        if not _is_public_address(peer_address):
            response.close()
            raise SourceRegistryError("web source connected to a private or local address")
    return response


def bounded_source_registry_web_timeout(value: float) -> float:
    """Clamp public source-registry fetch waits to the finite registry bound."""

    try:
        requested = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("source registry web timeout must be numeric") from exc
    if not math.isfinite(requested):
        raise ValueError("source registry web timeout must be finite")
    return max(min(requested, float(SOURCE_REGISTRY_WEB_TIMEOUT_SECONDS)), 1.0)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SourceRegistryError(f"expected JSON object: {path}")
    return payload


def _json_write_atomic(path: Path, payload: dict[str, Any]) -> None:
    safe_payload = _redact_persisted_payload(payload)
    # lgtm [py/clear-text-storage-sensitive-data]
    replace_bytes_safely(
        path,
        (json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


_SENSITIVE_KEY_RE = re.compile(r"(?i)(?:token|secret|password|credential|authorization|private[_-]?key|api[_-]?key)")


def _redact_source_url(value: str) -> str:
    """Keep source identity while removing query material from persisted evidence."""

    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "[REDACTED]"
    if not parsed.query:
        return raw
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _redact_persisted_payload(value: Any, *, key: str = "") -> Any:
    """Keep registry receipts useful without persisting credential material."""

    if key.casefold() == "source_url" and isinstance(value, str):
        return _redact_source_url(value)
    if _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _redact_persisted_payload(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact_persisted_payload(item, key=key) for item in value]
    if isinstance(value, str):
        for pattern in CREDENTIAL_PATTERNS.values():
            if pattern.search(value.encode("utf-8", errors="ignore")):
                return "[REDACTED]"
    return value


def _permission_defaults() -> dict[str, Any]:
    """Return a fresh deny-by-default permission metadata record."""

    return {
        "permission_status": "not-mapped",
        "permission_evidence_ref": None,
        "rightsholder": None,
        "covered_scope": None,
        "covered_files": [],
        "covered_capabilities": [],
        "third_party_exclusions": [],
        "permission_checked_at": None,
    }


def _permission_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Read permission metadata while safely normalizing legacy v1 records."""

    defaults = _permission_defaults()
    for key in PERMISSION_FIELDS:
        if key in record:
            defaults[key] = record[key]
    return defaults


def _validate_permission_metadata(record: dict[str, Any], *, source_id: str, label: str) -> None:
    metadata = _permission_metadata(record)
    status = metadata["permission_status"]
    if status not in PERMISSION_STATUS_VALUES:
        raise SourceRegistryError(f"{source_id}: unsupported {label} permission_status: {status!r}")
    for key in ("covered_files", "covered_capabilities", "third_party_exclusions"):
        if not isinstance(metadata[key], list) or any(not isinstance(item, str) or not item.strip() for item in metadata[key]):
            raise SourceRegistryError(f"{source_id}: {label} {key} must be a list of non-empty strings")
    for key in ("permission_evidence_ref", "rightsholder", "permission_checked_at"):
        value = metadata[key]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise SourceRegistryError(f"{source_id}: {label} {key} must be null or a non-empty string")
    if metadata["covered_scope"] is not None and not isinstance(metadata["covered_scope"], str | dict):
        raise SourceRegistryError(f"{source_id}: {label} covered_scope must be null, a string or an object")
    if status == "written-permission":
        required = ("permission_evidence_ref", "rightsholder", "permission_checked_at")
        missing = [key for key in required if not metadata[key]]
        if missing:
            raise SourceRegistryError(f"{source_id}: written permission missing {label} values {missing}")
        if metadata["covered_scope"] is None and not metadata["covered_files"] and not metadata["covered_capabilities"]:
            raise SourceRegistryError(f"{source_id}: written permission requires covered scope, files or capabilities")


def _validate_copy_scope(record: dict[str, Any], *, source_id: str, label: str) -> None:
    """Allow executable copy only for an explicit, narrow transfer lane."""

    if record.get("code_copy_allowed") is not True:
        return
    mode = str(record.get("transfer_mode") or "")
    if mode != "direct-transfer-scoped":
        raise SourceRegistryError(f"{source_id}: {label} code_copy_allowed violates clean-room boundary; requires direct-transfer-scoped mode")
    if record.get("permission_status") != "written-permission":
        raise SourceRegistryError(f"{source_id}: {label} code_copy_allowed requires written permission")
    covered_files = record.get("covered_files")
    if not isinstance(covered_files, list) or not covered_files:
        raise SourceRegistryError(f"{source_id}: {label} code_copy_allowed requires covered_files")
    scope = record.get("covered_scope")
    if not isinstance(scope, dict) or scope.get("code_transfer") != "authorized-for-covered-files":
        raise SourceRegistryError(f"{source_id}: {label} code_copy_allowed requires authorized covered_scope")
    exclusions = record.get("third_party_exclusions")
    if not isinstance(exclusions, list) or not exclusions:
        raise SourceRegistryError(f"{source_id}: {label} code_copy_allowed requires third_party_exclusions")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _classify_license_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unreadable"
    for kind, signature in LICENSE_SIGNATURES.items():
        if signature in text:
            return kind
    return "unclassified"


def _expected_license_kind(source: dict[str, Any]) -> str | None:
    if source.get("license_status") not in {"permissive", "source-available", "copyleft"}:
        return None
    declared = str(source.get("license", "")).lower()
    if declared.startswith("mit"):
        return "MIT-like"
    if declared.startswith("apache-2.0"):
        return "Apache-2.0"
    if declared.startswith("agpl-3.0"):
        return "AGPL-3.0"
    if declared.startswith("elastic-2.0"):
        return "Elastic-2.0"
    return None


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = PROCESS_EXECUTION_SOURCE_REGISTRY_GIT_TIMEOUT_SECONDS,
) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SourceRegistryError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


