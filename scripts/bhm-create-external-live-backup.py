#!/usr/bin/env python
"""Create a verified, portable backup of BHM's active local authority.

The command uses SQLite's online-backup API, so it never copies a live
``-wal`` file.  Its destination must be an empty directory outside the active
runtime root.  The generated manifest contains the integrity evidence needed
for a later restore decision; it intentionally does not perform restoration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / ".runtime" / "live-memory"
SQLITE_NAMES = ("memories.sqlite3", "observations.sqlite3", "hook-jobs.sqlite3")
JSON_GLOBS = ("*.json",)


class ExternalBackupError(RuntimeError):
    """Raised when a requested backup boundary is unsafe."""


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_snapshot(source: Path, destination: Path) -> dict[str, Any]:
    if not source.is_file() or _is_reparse_point(source):
        raise ExternalBackupError(f"unsafe SQLite source: {source}")
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30.0)
    destination_connection = sqlite3.connect(str(destination), timeout=30.0)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    with sqlite3.connect(f"file:{destination.as_posix()}?mode=ro", uri=True, timeout=30.0) as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    if quick_check != "ok" or foreign_key_errors:
        raise ExternalBackupError(f"SQLite verification failed for {destination.name}: {quick_check}, fk={foreign_key_errors}")
    return {
        "source": source.name,
        "backup": destination.name,
        "bytes": destination.stat().st_size,
        "sha256": _sha256(destination),
        "quick_check": quick_check,
        "foreign_key_errors": foreign_key_errors,
        "page_count": page_count,
    }


def _copy_json(source: Path, destination: Path) -> dict[str, Any]:
    if _is_reparse_point(source):
        raise ExternalBackupError(f"reparse-point JSON source rejected: {source}")
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "source": source.name,
        "backup": destination.name,
        "bytes": destination.stat().st_size,
        "sha256": _sha256(destination),
    }


def create_backup(source_root: Path, destination_root: Path) -> dict[str, Any]:
    source_root = source_root.resolve(strict=True)
    destination_root = destination_root.resolve(strict=False)
    if not source_root.is_dir() or _is_reparse_point(source_root):
        raise ExternalBackupError(f"unsafe source root: {source_root}")
    if destination_root.exists():
        raise ExternalBackupError(f"destination must not exist: {destination_root}")
    if destination_root == source_root or source_root in destination_root.parents or destination_root in source_root.parents:
        raise ExternalBackupError("destination must be outside the active live-memory root")
    parent = destination_root.parent.resolve(strict=True)
    if _is_reparse_point(parent):
        raise ExternalBackupError(f"destination parent is a reparse point: {parent}")

    destination_root.mkdir(parents=False)
    try:
        databases = []
        for name in SQLITE_NAMES:
            databases.append(_sqlite_snapshot(source_root / name, destination_root / name))
        copied_json = []
        for pattern in JSON_GLOBS:
            for source in sorted(source_root.glob(pattern), key=lambda path: path.name.casefold()):
                copied_json.append(_copy_json(source, destination_root / source.name))
        manifest = {
            "schemaVersion": "bhm.external-live-backup.v1",
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "source_root": str(source_root),
            "destination_root": str(destination_root),
            "sqlite_online_backups": databases,
            "json_copies": copied_json,
            "recovery": "Stop BHM, preserve the current live-memory directory, then restore only after an explicit operator decision.",
        }
        manifest["manifest_digest"] = hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        (destination_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return manifest
    except Exception:
        shutil.rmtree(destination_root, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, required=True, help="new empty directory on an independent volume")
    args = parser.parse_args(argv)
    try:
        report = create_backup(args.source, args.destination)
    except (ExternalBackupError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
