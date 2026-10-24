"""Launcher environment helpers whose defaults depend on initialized roots."""

from __future__ import annotations

from pathlib import Path

from bhm_launcher_host import launcher as _launcher


def venv_python(root: Path = _launcher.PROJECT_ROOT) -> Path:
    if _launcher.os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def has_virtualenv(root: Path = _launcher.PROJECT_ROOT) -> bool:
    return (root / ".venv").is_dir()
