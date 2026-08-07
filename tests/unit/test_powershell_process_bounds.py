from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT.parent / "scripts" / "test-bhm-crystallize-worker.ps1"


def test_crystallizer_runner_uses_bounded_cleanup_wait() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "[ValidateRange(1, 60)][int]$CleanupTimeoutSeconds = 5" in source
    assert "$process.WaitForExit($CleanupTimeoutSeconds * 1000)" in source
    assert "$process.WaitForExit()" not in source
    assert "Stop-Process -Id $process.Id -Force" in source
