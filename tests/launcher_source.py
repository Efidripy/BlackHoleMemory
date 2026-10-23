"""Test helper: the launcher is split across scripts/bhm_launcher*.py; source-text contract tests read all of them."""

from __future__ import annotations

from pathlib import Path


def launcher_source_text(scripts_root: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(scripts_root.glob("bhm_launcher*.py")))
