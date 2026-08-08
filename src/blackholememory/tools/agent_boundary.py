"""Fail-closed path and media boundary for model-facing read tools."""

from __future__ import annotations

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_INPUT_ROOT = REPO_ROOT / ".runtime" / "agent-inputs"
_SENSITIVE_PARTS = frozenset(
    {
        ".git",
        ".src",
        ".ssh",
        "credentials",
        "secrets",
        "private",
        "live-memory",
        "logs",
        "__pycache__",
    }
)
_SENSITIVE_NAMES = frozenset({"id_rsa", "id_ed25519", "known_hosts", "credentials.json"})
_SENSITIVE_SUFFIXES = (
    ".env",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".secret",
    ".credentials",
)


def _configured_roots(
    extra_roots: tuple[Path, ...] = (),
    *,
    include_default_roots: bool = True,
) -> tuple[Path, ...]:
    roots = [REPO_ROOT, AGENT_INPUT_ROOT] if include_default_roots else []
    roots.extend(extra_roots)
    raw = os.getenv("BHM_AGENT_ALLOWED_ROOTS", "")
    if raw:
        roots.extend(Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip())
    result: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve(strict=False)
        except OSError:
            continue
        if resolved not in result:
            result.append(resolved)
    return tuple(result)


def _contains_sensitive_component(path: Path) -> bool:
    parts = {part.casefold() for part in path.parts}
    if parts & _SENSITIVE_PARTS:
        return True
    name = path.name.casefold()
    return name in _SENSITIVE_NAMES or any(name.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.anchor else Path()
    for part in path.parts:
        if part == path.anchor:
            continue
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_agent_path(
    value: str,
    *,
    allowed_roots: tuple[Path, ...] = (),
    include_default_roots: bool = True,
    max_bytes: int | None = None,
    require_regular_file: bool = True,
) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("path is required")
    candidate = Path(raw).expanduser()
    if _contains_sensitive_component(candidate):
        raise PermissionError("sensitive path is not available to model tools")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PermissionError(f"path resolution failed: {exc}") from exc
    if _has_symlink_component(candidate):
        raise PermissionError("symlink paths are not available to model tools")
    roots = _configured_roots(allowed_roots, include_default_roots=include_default_roots)
    if not any(_is_within(resolved, root) for root in roots):
        raise PermissionError("path is outside approved model-tool roots")
    if require_regular_file and not resolved.is_file():
        raise ValueError("path is not a regular file")
    if max_bytes is not None:
        size = resolved.stat().st_size
        if size > max(1, int(max_bytes)):
            raise ValueError(f"file is too large: {size} bytes > {int(max_bytes)} bytes")
    return resolved


def read_agent_bytes(path: Path, *, max_bytes: int | None = None) -> bytes:
    """Read a previously resolved model-tool path with identity revalidation.

    Resolution and opening are separate filesystem operations.  Re-check the
    path around the opened descriptor so a symlink/hardlink/rename swap cannot
    turn the validated path into an unrelated file between those operations.
    """

    candidate = Path(path)
    if _has_symlink_component(candidate):
        raise PermissionError("symlink paths are not available to model tools")
    try:
        before = candidate.stat()
    except OSError as exc:
        raise PermissionError(f"path inspection failed: {exc}") from exc
    if not candidate.is_file():
        raise ValueError("path is not a regular file")
    if max_bytes is not None and before.st_size > max(1, int(max_bytes)):
        raise ValueError(f"file is too large: {before.st_size} bytes > {int(max_bytes)} bytes")

    limit = max(1, int(max_bytes)) if max_bytes is not None else None
    try:
        with candidate.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise PermissionError("validated model-tool path changed before read")
            payload = handle.read(limit + 1 if limit is not None else -1)
    except PermissionError:
        raise
    except OSError as exc:
        raise PermissionError(f"path read failed: {exc}") from exc

    if limit is not None and len(payload) > limit:
        raise ValueError(f"file is too large: {len(payload)} bytes > {limit} bytes")
    if _has_symlink_component(candidate):
        raise PermissionError("symlink paths are not available to model tools")
    try:
        after = candidate.stat()
    except OSError as exc:
        raise PermissionError(f"path revalidation failed: {exc}") from exc
    if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino) or after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
        raise PermissionError("validated model-tool path changed during read")
    return payload


def read_agent_text(path: Path, *, max_bytes: int | None = None) -> str:
    return read_agent_bytes(path, max_bytes=max_bytes).decode("utf-8", errors="replace")


def vision_endpoint_allowed(base_url: str) -> bool:
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(str(base_url or ""))
        hostname = str(parsed.hostname or "").casefold()
    except ValueError:
        return False
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        return False
    configured = {
        item.strip().casefold()
        for item in os.getenv("BHM_AGENT_ALLOWED_VISION_HOSTS", "").split(",")
        if item.strip()
    }
    return hostname in {"127.0.0.1", "localhost", "::1"} or hostname in configured


def image_magic_matches(path: Path, payload: bytes) -> bool:
    suffix = path.suffix.casefold()
    if suffix == ".png":
        return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return payload.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"
    return False


__all__ = [
    "AGENT_INPUT_ROOT",
    "REPO_ROOT",
    "image_magic_matches",
    "read_agent_bytes",
    "read_agent_text",
    "resolve_agent_path",
    "vision_endpoint_allowed",
]
