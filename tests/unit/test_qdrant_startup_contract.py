from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_qdrant_startup_health_probe_is_finite_and_bounded() -> None:
    source = (ROOT / "scripts" / "start-qdrant.ps1").read_text(encoding="utf-8")
    assert "[ValidateRange(5, 300)][int]$TimeoutSec = 120" in source
    assert "$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)" in source
    assert "Invoke-WebRequest -UseBasicParsing -Uri $qdrantHealthUrl -TimeoutSec 5" in source
    assert "Qdrant did not become HTTP-ready" in source
    assert "Invoke-DockerBounded" in source
    assert "WaitForExit" in source
    assert "$dockerCommandTimeoutSec = 20" in source
    assert "Qdrant docker compose startup failed with exit code" in source
    assert "$dockerCommand = Get-Command docker" in source
    assert "$dockerExecutable" in source
    assert "Docker executable was not found" in source


def test_qdrant_recovery_exposes_safe_escalation_and_force_gate() -> None:
    source = (ROOT / "scripts" / "recover-qdrant.ps1").read_text(encoding="utf-8")
    assert "[switch]$Force" in source
    assert "[switch]$WhatIf" in source
    assert "Invoke-SoftRecovery" in source
    assert "Invoke-ForceRecovery" in source
    assert "Stop-Service -Name 'com.docker.service'" in source
    assert "wsl.exe --shutdown" in source
    assert "Start-Service -Name 'com.docker.service'" in source
    assert "docker system prune" not in source.casefold()
    assert "qdrant.recovery.v1" in source
    assert "Re-run with -Force" in source
    assert "$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)" in source
