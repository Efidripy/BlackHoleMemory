from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_projection_sidecar_keeps_authority_in_bhm_and_worker_in_shadow_mode():
    runner = (ROOT / "scripts" / "run-bhm-projection-sidecar.ps1").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "start-bhm-projection-sidecar.ps1").read_text(encoding="utf-8")
    authoritative = (ROOT / "scripts" / "start-bhm-authoritative.ps1").read_text(encoding="utf-8")

    assert "BHM_MEMORY_STORE_MODE = 'sqlite-shadow'" in runner
    assert "BHM_PROJECTION_WORKER_ENABLED = 'true'" in runner
    assert "run-bhm-projection-worker.py" in runner
    assert "--loop" in runner
    assert "--quiet-idle" in runner
    assert "$failureStreak" in runner
    assert "$workerExitCode -eq 75" in runner
    assert "$ErrorActionPreference = 'Continue'" in runner
    assert "infrastructure_recovered" in runner
    assert "projection-sidecar.pid" in runner
    assert "projection-sidecar.stop" in launcher
    assert "function Get-PidFileProcess" in launcher
    assert "already-running" in launcher
    assert "Stop-Process -Id $pidProcess.Id" in launcher
    assert "start-bhm-projection-sidecar.ps1" in authoritative
    assert "BHM_MEMORY_STORE_MODE = 'sqlite-authoritative'" not in runner
    assert "start-qdrant.ps1" not in runner
