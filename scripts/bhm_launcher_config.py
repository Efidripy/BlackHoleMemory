"""Validated, backup-aware and atomic launcher settings persistence."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bhm_runtime_endpoints import endpoint_port


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
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    backup_path = backup_dir / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, backup_path)
    return backup_path


def load_settings(path: Path, *, backup_dir: Path) -> ConfigResult:
    path = Path(path)
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
        backup = _backup_existing(path, Path(backup_dir))
        backup_path = str(backup) if backup else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(settings, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    except OSError as exc:
        raise OSError(f"launcher settings save failed; backup={backup_path or 'none'}: {exc}") from exc
    return ConfigResult(ok=True, settings=settings, backup_path=backup_path)


__all__ = ["ConfigResult", "load_settings", "save_settings", "validate_settings"]
