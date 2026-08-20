from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_frozen_launcher_keeps_sqlite_authoritative_api_independent_of_qdrant():
    launcher = (REPO_ROOT / "scripts" / "bhm_launcher.py").read_text(encoding="utf-8")
    authoritative = (REPO_ROOT / "scripts" / "start-bhm-authoritative.ps1").read_text(encoding="utf-8")
    service = (REPO_ROOT / "scripts" / "run-service.ps1").read_text(encoding="utf-8")

    assert 'script = project_root / "scripts" / "start-bhm-authoritative.ps1"' in launcher
    assert 'args.append("-ForceRestart")' in launcher
    assert '"-SkipProjectionRecovery"' in launcher
    assert 'args.append("-StopOnly")' in launcher
    assert 'START DEFERRED: api waiting for Qdrant HTTP readiness' not in launcher
    assert 'self.start_service("qdrant", on_success=lambda: self.start_service("api"))' not in launcher
    assert "if card and operation and operation.isRunning():" in launcher
    assert "[switch]$StopOnly" in authoritative
    assert "[switch]$SkipProjectionRecovery" in authoritative
    assert "Wait-AuthoritativeQdrant" not in service
    assert 'BHM_QDRANT_REQUIRED_FOR_CORE = "false"' in service
    assert "[switch]$SemanticFusion" in service
    assert 'BHM_CODE_SEMANTIC_FUSION = "1"' in service
    assert 'BHM_MEMORY_STORE_MODE = "sqlite-authoritative"' in service
    assert "Resolve-AuthoritativeProviderEndpoint" in service
    assert "Assert-BhmApiLoopbackHost" in service
    assert 'Get-BhmRuntimeEndpoint -Name "lm_studio"' in service
    assert 'Get-BhmRuntimeEndpointParts -Name "lm_studio"' in service
    assert "172\\.18\\.0\\.1:13666/v1" not in service
    assert "127.0.0.1:13666/v1" not in service
    assert "Resolve-Path -LiteralPath $ProjectRoot" in service


def test_launcher_does_not_select_project_root_from_process_cwd():
    launcher = (REPO_ROOT / "scripts" / "bhm_launcher.py").read_text(encoding="utf-8")

    assert "roots.append(Path.cwd().resolve())" not in launcher
