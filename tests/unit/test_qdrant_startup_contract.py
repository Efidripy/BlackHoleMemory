from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_qdrant_startup_health_probe_is_finite_and_bounded() -> None:
    source = (ROOT / "scripts" / "start-qdrant.ps1").read_text(encoding="utf-8")
    assert "[ValidateRange(1, 30)][int]$TimeoutSec = 30" in source
    assert "-TimeoutSec $TimeoutSec" in source
    assert "Invoke-WebRequest -UseBasicParsing $qdrantHealthUrl" not in source
