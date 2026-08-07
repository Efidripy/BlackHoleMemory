from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _source(name: str) -> str:
    return (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_authoritative_launcher_shutdown_is_bounded() -> None:
    source = _source("start-bhm-authoritative.ps1")
    assert "[ValidateRange(1, 60)][int]$ShutdownTimeoutSec = 5" in source
    assert "Get-Process -Id $_ -ErrorAction Stop" in source
    assert "$retryDeadline = [DateTime]::UtcNow.AddSeconds($ShutdownTimeoutSec)" in source
    assert "BHM process cleanup exceeded bounded shutdown deadline" in source


def test_workspace_launcher_shutdown_is_bounded() -> None:
    source = _source("start-bhm-workspace.ps1")
    assert "[ValidateRange(1, 60)][int]$ShutdownTimeoutSec = 5" in source
    assert "BHM workspace process cleanup exceeded bounded shutdown deadline" in source
    assert "Get-Process -Id $_ -ErrorAction Stop" in source


def test_projection_operator_cleans_up_timed_out_launcher() -> None:
    source = _source("bhm-projection-operator.ps1")
    assert "$ProjectionShutdownTimeoutSec = 5" in source
    assert "$timedOut = -not $launcher.HasExited" in source
    assert "Stop-Process -Id $launcher.Id -Force" in source
    assert "timed_out = $timedOut" in source


def test_portable_validator_cleanup_has_bounded_retry() -> None:
    source = _source("validate-bhm-portable-install.ps1")
    assert "[ValidateRange(1, 60)][int]$CleanupTimeoutSeconds = 5" in source
    assert "function Stop-PortableProcessBounded" in source
    assert "$retryDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)" in source
    assert "Portable runtime process cleanup exceeded bounded deadline" in source
