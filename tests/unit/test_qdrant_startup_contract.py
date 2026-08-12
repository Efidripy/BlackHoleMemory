from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_qdrant_startup_health_probe_is_finite_and_bounded() -> None:
    source = (ROOT / "scripts" / "start-qdrant.ps1").read_text(encoding="utf-8")
    assert "[ValidateRange(5, 300)][int]$TimeoutSec = 120" in source
    assert "$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)" in source
    assert "Invoke-WebRequest -UseBasicParsing -Uri $qdrantHealthUrl -TimeoutSec 5" in source
    assert "Qdrant did not become HTTP-ready" in source
    assert "$composeExitCode = $LASTEXITCODE" in source
    assert "Qdrant docker compose startup failed with exit code" in source
