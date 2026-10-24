"""Offline, quarantine-only document parser admission prototype.

This module intentionally supports only UTF-8 ``.txt`` files under explicit
local roots.  It neither fetches URLs nor persists parsed text.  Its result is
a digest-only receipt for a separately governed quarantine workflow; it is not
an authority write request and exposes no REST/MCP/UI surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .filesystem_boundaries import FilesystemBoundaryError, FilesystemReadLimitError, assert_safe_path, read_bytes_safely


SCHEMA_VERSION = "bhm.document-parser-quarantine.v1"
DEFAULT_ALLOWED_EXTENSIONS = frozenset({".txt"})
DEFAULT_MAX_BYTES = 1_048_576
DEFAULT_MAX_PAGES = 32
DEFAULT_TIMEOUT_SECONDS = 2.0
MAX_TIMEOUT_SECONDS = 10.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


class DocumentParserAdapterError(ValueError):
    """Raised when an adapter request cannot safely produce a receipt."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _path_digest(path: Path) -> str:
    return hashlib.sha256(os.fspath(path).encode("utf-8")).hexdigest()


def _normalise_project(project: Any) -> str:
    normalized = str(project or "").strip()
    if not _PROJECT.fullmatch(normalized):
        raise DocumentParserAdapterError("project is invalid")
    return normalized


def _normalise_extensions(extensions: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({str(value).strip().casefold() for value in extensions}))
    if not normalized or any(not value.startswith(".") or len(value) > 16 or not value[1:].isalnum() for value in normalized):
        raise DocumentParserAdapterError("allowed extensions are invalid")
    return normalized


def _safe_root(value: Path | str) -> Path:
    root = Path(os.path.abspath(os.fspath(value)))
    try:
        checked = assert_safe_path(root, reject_hardlink_target=False)
    except FilesystemBoundaryError as exc:
        raise DocumentParserAdapterError("allowlisted root crosses an unsafe filesystem boundary") from exc
    if not checked.is_dir():
        raise DocumentParserAdapterError("allowlisted root is not a directory")
    return checked


def _is_under_root(candidate: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath((os.path.normcase(os.fspath(candidate)), os.path.normcase(os.fspath(root))))
    except ValueError:
        return False
    return common == os.path.normcase(os.fspath(root))


def _policy(
    *,
    allowed_roots: Sequence[Path | str],
    allowed_extensions: Sequence[str],
    max_bytes: int,
    max_pages: int,
    timeout_seconds: float,
) -> tuple[tuple[Path, ...], tuple[str, ...], dict[str, Any]]:
    if not allowed_roots or len(allowed_roots) > 16:
        raise DocumentParserAdapterError("allowlisted roots are invalid")
    roots = tuple(sorted({_safe_root(value) for value in allowed_roots}, key=lambda value: os.path.normcase(os.fspath(value))))
    extensions = _normalise_extensions(allowed_extensions)
    if not 1 <= max_bytes <= DEFAULT_MAX_BYTES:
        raise DocumentParserAdapterError("max_bytes is outside the safe prototype bound")
    if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
        raise DocumentParserAdapterError("max_pages is outside the safe prototype bound")
    if not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise DocumentParserAdapterError("timeout_seconds is outside the safe prototype bound")
    metadata = {
        "allowed_root_digests": [_path_digest(root) for root in roots],
        "allowed_extensions": list(extensions),
        "max_bytes": max_bytes,
        "max_pages": max_pages,
        "timeout_seconds": timeout_seconds,
    }
    return roots, extensions, metadata


def _receipt(
    *,
    project: str,
    locator: Path,
    policy: Mapping[str, Any],
    status: str,
    reason: str,
    content_sha256: str | None = None,
    byte_count: int | None = None,
    page_count: int | None = None,
) -> dict[str, Any]:
    core = {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "source_locator_digest": _path_digest(locator),
        "policy_digest": _digest(policy),
        "status": status,
        "reason": reason,
        "content_sha256": content_sha256,
        "byte_count": byte_count,
        "page_count": page_count,
    }
    return {
        **core,
        "receipt_digest": _digest(core),
        "quarantine": {
            "required": True,
            "persisted": False,
            "raw_content_retained": False,
            "operator_handoff_required": True,
        },
        "execution": {
            "dry_run": True,
            "offline_only": True,
            "url_fetch": False,
            "sqlite_mutation": False,
            "outbox_mutation": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "graph_mutation": False,
            "direct_authority_write": False,
        },
        "rollback": "discard-receipt-no-authority-or-projection-state-was-mutated",
    }


def inspect_document_parser_candidate(
    source_path: Path | str,
    *,
    project: str,
    allowed_roots: Sequence[Path | str],
    operator_confirmed: bool = False,
    allowed_extensions: Sequence[str] = tuple(DEFAULT_ALLOWED_EXTENSIONS),
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Inspect one local text candidate and return a content-free receipt.

    The function has no apply mode.  ``operator_confirmed`` only permits a
    bounded read for this dry-run prototype; it never authorizes persistence.
    Unsafe input is quarantined without reading its bytes where possible.
    """

    normalized_project = _normalise_project(project)
    raw_source = os.fspath(source_path)
    if "://" in raw_source or raw_source.casefold().startswith(("file:", "http:", "https:")):
        raise DocumentParserAdapterError("URLs and URI inputs are not supported")
    candidate = Path(os.path.abspath(raw_source))
    roots, extensions, policy = _policy(
        allowed_roots=allowed_roots,
        allowed_extensions=allowed_extensions,
        max_bytes=max_bytes,
        max_pages=max_pages,
        timeout_seconds=timeout_seconds,
    )
    if not operator_confirmed:
        return _receipt(
            project=normalized_project,
            locator=candidate,
            policy=policy,
            status="blocked",
            reason="operator_confirmation_required",
        )
    if not any(_is_under_root(candidate, root) for root in roots):
        return _receipt(project=normalized_project, locator=candidate, policy=policy, status="quarantined", reason="root_not_allowlisted")
    if candidate.suffix.casefold() not in extensions:
        return _receipt(project=normalized_project, locator=candidate, policy=policy, status="quarantined", reason="type_not_allowlisted")
    started = time.monotonic()
    try:
        payload = read_bytes_safely(candidate, max_bytes=max_bytes)
    except FilesystemReadLimitError:
        return _receipt(project=normalized_project, locator=candidate, policy=policy, status="quarantined", reason="byte_budget_exceeded")
    except (FilesystemBoundaryError, OSError):
        return _receipt(project=normalized_project, locator=candidate, policy=policy, status="quarantined", reason="unsafe_filesystem_boundary")
    content_sha256 = hashlib.sha256(payload).hexdigest()
    try:
        decoded = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return _receipt(
            project=normalized_project,
            locator=candidate,
            policy=policy,
            status="quarantined",
            reason="unsupported_encoding",
            content_sha256=content_sha256,
            byte_count=len(payload),
        )
    page_count = decoded.count("\f") + 1
    if page_count > max_pages:
        return _receipt(
            project=normalized_project,
            locator=candidate,
            policy=policy,
            status="quarantined",
            reason="page_budget_exceeded",
            content_sha256=content_sha256,
            byte_count=len(payload),
            page_count=page_count,
        )
    if time.monotonic() - started > timeout_seconds:
        return _receipt(
            project=normalized_project,
            locator=candidate,
            policy=policy,
            status="quarantined",
            reason="time_budget_exceeded",
            content_sha256=content_sha256,
            byte_count=len(payload),
            page_count=page_count,
        )
    return _receipt(
        project=normalized_project,
        locator=candidate,
        policy=policy,
        status="quarantined",
        reason="review_required",
        content_sha256=content_sha256,
        byte_count=len(payload),
        page_count=page_count,
    )


def verify_document_parser_receipt(receipt: Mapping[str, Any]) -> bool:
    """Verify the digest and immutable no-mutation contract of one receipt."""

    core = {
        key: receipt.get(key)
        for key in (
            "schema_version",
            "project",
            "source_locator_digest",
            "policy_digest",
            "status",
            "reason",
            "content_sha256",
            "byte_count",
            "page_count",
        )
    }
    try:
        _normalise_project(core["project"])
        if not _SHA256.fullmatch(str(core["source_locator_digest"] or "")) or not _SHA256.fullmatch(str(core["policy_digest"] or "")):
            return False
        if core["content_sha256"] is not None and not _SHA256.fullmatch(str(core["content_sha256"])):
            return False
    except DocumentParserAdapterError:
        return False
    return (
        core["schema_version"] == SCHEMA_VERSION
        and core["status"] in {"blocked", "quarantined"}
        and receipt.get("receipt_digest") == _digest(core)
        and receipt.get("quarantine")
        == {"required": True, "persisted": False, "raw_content_retained": False, "operator_handoff_required": True}
        and receipt.get("execution")
        == {
            "dry_run": True,
            "offline_only": True,
            "url_fetch": False,
            "sqlite_mutation": False,
            "outbox_mutation": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "graph_mutation": False,
            "direct_authority_write": False,
        }
        and receipt.get("rollback") == "discard-receipt-no-authority-or-projection-state-was-mutated"
    )


__all__ = [
    "DEFAULT_ALLOWED_EXTENSIONS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_TIMEOUT_SECONDS",
    "DocumentParserAdapterError",
    "SCHEMA_VERSION",
    "inspect_document_parser_candidate",
    "verify_document_parser_receipt",
]
