from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_frozen_launcher_passes_real_project_root_to_api_service_script():
    launcher = (REPO_ROOT / "scripts" / "bhm_launcher.py").read_text(encoding="utf-8")
    service = (REPO_ROOT / "scripts" / "run-service.ps1").read_text(encoding="utf-8")

    assert 'canonical_script = project_root / "scripts" / "run-service.ps1"' in launcher
    assert '"-ProjectRoot",' in launcher
    assert '"-Authoritative"' in launcher
    assert "[string]$ProjectRoot = ''" in service
    assert "[switch]$Authoritative" in service
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
