"""Fail-closed local filesystem path checks for write-capable boundaries."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


class FilesystemBoundaryError(OSError):
    """Raised when a path crosses an unexpected local filesystem boundary."""


def assert_safe_path(path: Path | str, *, reject_hardlink_target: bool = True) -> Path:
    """Inspect existing components without following reparse points."""

    raw_path = os.fspath(path)
    if os.name == "nt":
        normalized = raw_path.replace("/", "\\")
        if normalized.startswith("\\\\") and not normalized.startswith("\\\\?\\"):
            raise FilesystemBoundaryError("UNC filesystem paths are not allowed")
        if normalized.casefold().startswith("\\\\?\\unc\\"):
            raise FilesystemBoundaryError("extended UNC filesystem paths are not allowed")
    candidate = Path(os.path.abspath(raw_path))
    current = candidate
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            info = None
        except OSError as exc:
            raise FilesystemBoundaryError(f"unable to inspect filesystem path: {current}") from exc
        if info is not None:
            attributes = int(getattr(info, "st_file_attributes", 0))
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if stat.S_ISLNK(info.st_mode) or (reparse_flag and attributes & reparse_flag):
                raise FilesystemBoundaryError(f"filesystem path contains a symlink/junction/reparse point: {current}")
            if current == candidate:
                if reject_hardlink_target and stat.S_ISREG(info.st_mode) and int(getattr(info, "st_nlink", 1)) > 1:
                    raise FilesystemBoundaryError(f"filesystem target is a hardlink: {current}")
            elif not stat.S_ISDIR(info.st_mode):
                raise FilesystemBoundaryError(f"filesystem path component is not a directory: {current}")
        if current.parent == current:
            return candidate
        current = current.parent


def write_bytes_exclusive(path: Path | str, payload: bytes) -> Path:
    """Create a bounded file without truncating a raced or linked target.

    The caller may safely retry an identical deterministic write: an existing
    regular file with byte-identical content is accepted, while a hardlink,
    reparse point, or different file fails closed.  The exclusive create is
    the important boundary between the preflight check and the actual write.
    """

    target = assert_safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    assert_safe_path(target.parent, reject_hardlink_target=False)
    try:
        with target.open("xb") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        # Re-check the now-existing target before reading it.  This rejects a
        # hardlink/reparse target instead of silently truncating it.
        assert_safe_path(target)
        try:
            existing = target.read_bytes()
        except OSError as read_exc:
            raise FilesystemBoundaryError(f"unable to verify existing filesystem target: {target}") from read_exc
        if existing != payload:
            raise FilesystemBoundaryError(f"filesystem target already exists with different content: {target}") from exc
    return target


def append_bytes_safely(path: Path | str, payload: bytes) -> Path:
    """Append to a local regular file without accepting linked targets.

    The target is checked before open and the opened descriptor is compared
    with a second ``lstat`` before any bytes are written.  This is intended
    for bounded append-only logs where atomic replacement is not appropriate.
    """

    target = assert_safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    assert_safe_path(target.parent, reject_hardlink_target=False)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(target, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        current = target.lstat()
        if not stat.S_ISREG(current.st_mode):
            raise FilesystemBoundaryError(f"filesystem target is not a regular file: {target}")
        if int(getattr(current, "st_nlink", 1)) > 1:
            raise FilesystemBoundaryError(f"filesystem target is a hardlink: {target}")
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise FilesystemBoundaryError(f"filesystem target changed during append: {target}")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise FilesystemBoundaryError(f"filesystem append made no progress: {target}")
            view = view[written:]
    finally:
        os.close(descriptor)
    return target


def replace_bytes_safely(path: Path | str, payload: bytes) -> Path:
    """Atomically replace a regular target after boundary checks.

    The temporary file is created in the already-validated parent directory;
    the target is revalidated before replacement so a hardlink/reparse target
    cannot be silently truncated.  This is intended for metadata whose bytes
    legitimately change between deterministic exports (for example a receipt
    timestamp), while preserving the same local-only boundary contract.
    """

    target = assert_safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    assert_safe_path(target.parent, reject_hardlink_target=False)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        assert_safe_path(temporary)
        if target.exists():
            assert_safe_path(target)
        assert_safe_path(target.parent, reject_hardlink_target=False)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return target


__all__ = [
    "append_bytes_safely",
    "FilesystemBoundaryError",
    "assert_safe_path",
    "replace_bytes_safely",
    "write_bytes_exclusive",
]
