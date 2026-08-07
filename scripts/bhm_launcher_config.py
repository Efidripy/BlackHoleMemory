"""Validated, backup-aware and atomic launcher settings persistence."""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bhm_runtime_endpoints import endpoint_port
from blackholememory.filesystem_boundaries import replace_bytes_safely


@dataclass(frozen=True)
class ConfigResult:
    ok: bool
    settings: dict[str, Any]
    error: str = ""
    backup_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "settings": self.settings,
            "error": self.error,
            "backup_path": self.backup_path,
        }


def validate_settings(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("launcher settings root must be an object")
    candidate = dict(payload)
    llm = candidate.get("llm")
    if llm is not None:
        if not isinstance(llm, dict):
            raise ValueError("launcher settings llm must be an object")
        mode = llm.get("mode", "local")
        if mode not in {"local", "remote"}:
            raise ValueError("launcher settings llm.mode must be local or remote")
        port = llm.get("port", endpoint_port("llm_default"))
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("launcher settings llm.port must be between 1 and 65535")
        remote_url = llm.get("remote_url", "")
        if not isinstance(remote_url, str):
            raise ValueError("launcher settings llm.remote_url must be a string")
    json.dumps(candidate, ensure_ascii=False)
    return candidate


def _backup_existing(path: Path, backup_dir: Path) -> Path | None:
    _assert_safe_path(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise OSError(f"launcher settings target is not a regular file: {path}")
    _assert_safe_path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    _assert_safe_path(backup_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    backup_path = backup_dir / f"{path.name}.{stamp}.bak"
    _assert_safe_path(backup_path)
    shutil.copy2(path, backup_path)
    return backup_path


def load_settings(path: Path, *, backup_dir: Path) -> ConfigResult:
    path = Path(path)
    try:
        _assert_safe_path(path)
    except OSError as exc:
        return ConfigResult(ok=False, settings={}, error=str(exc)[:400])
    if not path.exists():
        return ConfigResult(ok=True, settings={})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        settings = validate_settings(payload)
        return ConfigResult(ok=True, settings=settings)
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        backup_path = ""
        try:
            backup = _backup_existing(path, Path(backup_dir))
            backup_path = str(backup) if backup else ""
        except OSError as backup_exc:
            backup_path = f"backup failed: {backup_exc}"
        return ConfigResult(
            ok=False,
            settings={},
            error=str(exc)[:400],
            backup_path=backup_path,
        )


def save_settings(path: Path, payload: Any, *, backup_dir: Path) -> ConfigResult:
    path = Path(path)
    settings = validate_settings(payload)
    backup_path = ""
    try:
        _assert_safe_path(path)
        _assert_safe_path(path.parent)
        backup = _backup_existing(path, Path(backup_dir))
        backup_path = str(backup) if backup else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_path(path.parent)
        content = (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        replace_bytes_safely(path, content)
    except OSError as exc:
        raise OSError(f"launcher settings save failed; backup={backup_path or 'none'}: {exc}") from exc
    return ConfigResult(ok=True, settings=settings, backup_path=backup_path)


def _assert_safe_path(path: Path) -> None:
    """Reject reparse/symlink components and hardlinked file targets."""

    candidate = Path(os.path.abspath(os.fspath(path)))
    current = candidate
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            info = None
        except OSError as exc:
            raise OSError(f"unable to inspect launcher settings path: {current}") from exc
        if info is not None:
            attributes = int(getattr(info, "st_file_attributes", 0))
            if stat.S_ISLNK(info.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise OSError(f"launcher settings path contains a symlink/junction/reparse point: {current}")
            if current == candidate:
                if stat.S_ISREG(info.st_mode) and int(getattr(info, "st_nlink", 1)) > 1:
                    raise OSError(f"launcher settings target is a hardlink: {current}")
            elif not stat.S_ISDIR(info.st_mode):
                raise OSError(f"launcher settings path component is not a directory: {current}")
        if current.parent == current:
            return
        current = current.parent


__all__ = ["ConfigResult", "load_settings", "save_settings", "validate_settings"]
