from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "scripts" / "start-bhm-authoritative.ps1"
SERVICE = REPO_ROOT / "scripts" / "run-service.ps1"
RUNTIME_ENDPOINTS = REPO_ROOT / "scripts" / "runtime-endpoints.ps1"


def test_authoritative_launcher_contract_is_fail_closed_and_explicit():
    text = LAUNCHER.read_text(encoding="utf-8")

    for marker in (
        'BHM_MEMORY_STORE_MODE = "sqlite-authoritative"',
        'BHM_QDRANT_REQUIRED_FOR_CORE = "false"',
        'BHM_FALLBACK_MODE = "explicit"',
        'BHM_PROJECTION_WORKER_ENABLED = "false"',
        'BHM_MEMORY_STORE_PARITY_CONFIRMED = "true"',
        'BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED = "true"',
        "/health/ready",
        "/bhm/health",
        "/health/cutover",
        "projection-only",
        "projectionStatusAllowed",
        "'degraded'",
        "direct_vector_writes",
        "Wait-Authoritative",
        "ProbeOnly",
        "ForceRestart",
        "SemanticFusion",
        "-SemanticFusion",
        "BaseUrl",
        "Stop-BhmProcesses",
        "Stop-ProjectionSidecar",
        "The API port is the authoritative fallback",
        "Stop-Process -Id ([int]$listenerId)",
        "run-service.ps1",
        "'-Authoritative'",
        "start-qdrant.ps1",
        "QdrantTimeoutSec",
        "Qdrant projection recovery is pending; BHM core remains ready.",
        "rolled-back",
        "Resolve-LocalLmStudioEndpoint",
        "Assert-BhmApiLoopbackHost",
        "Test-OpenAiBaseUrl",
        "Get-BhmRuntimeEndpoint -Name 'lm_studio'",
        "Get-BhmRuntimeEndpointParts -Name 'lm_studio'",
        "$lmStudioParts.Port",
    ):
        assert marker in text

    assert "172\\.18\\.0\\.1:13666/v1" not in text
    assert "127.0.0.1:13666/v1" not in text

    api_start = text.index("Start-BhmDetachedHidden -FilePath 'powershell.exe'")
    qdrant_recovery = text.index("$qdrantOutput = @(", api_start)
    assert api_start < qdrant_recovery


def test_workspace_launcher_delegates_to_canonical_authoritative_startup() -> None:
    text = (REPO_ROOT / "scripts" / "start-bhm-workspace.ps1").read_text(encoding="utf-8")

    assert 'scripts\\start-bhm-authoritative.ps1' in text
    assert 'scripts\\run-service.ps1' not in text
    assert 'scripts\\start-qdrant.ps1' not in text


def test_operator_launcher_probe_timeouts_have_one_explicit_owner() -> None:
    endpoints = RUNTIME_ENDPOINTS.read_text(encoding="utf-8")
    authoritative = LAUNCHER.read_text(encoding="utf-8")
    service = SERVICE.read_text(encoding="utf-8")
    workspace = (REPO_ROOT / "scripts" / "start-bhm-workspace.ps1").read_text(encoding="utf-8")

    for name, seconds in (
        ("local_llm_models", 2),
        ("authoritative_health", 10),
        ("authoritative_ready", 3),
        ("workspace_availability", 2),
    ):
        assert f"{name} = {seconds}" in endpoints
        assert f"'{name}'" in endpoints

    assert "Get-BhmOperatorProbeTimeout -Name 'local_llm_models'" in authoritative
    assert "Get-BhmOperatorProbeTimeout -Name 'authoritative_health'" in authoritative
    assert "Get-BhmOperatorProbeTimeout -Name 'authoritative_ready'" in authoritative
    assert "Get-BhmOperatorProbeTimeout -Name 'local_llm_models'" in service
    assert "Get-BhmOperatorProbeTimeout -Name 'workspace_availability'" in workspace
    assert "-TimeoutSec 10" not in authoritative
    assert "-TimeoutSec 3" not in authoritative
    assert "-TimeoutSec 2" not in authoritative
    assert "-TimeoutSec 2" not in service
    assert "-TimeoutSec 2" not in workspace


def test_authoritative_launcher_readiness_deadline_encloses_each_probe() -> None:
    authoritative = LAUNCHER.read_text(encoding="utf-8")

    assert "[ValidateRange(5, 300)][int]$TimeoutSec = 120" in authoritative
    assert "$remainingSeconds = [Math]::Floor(($deadline - [DateTime]::UtcNow).TotalSeconds)" in authoritative
    assert "$readyProbeTimeoutSec = [Math]::Min($authoritativeReadyProbeTimeoutSec, [int]$remainingSeconds)" in authoritative
    assert "$contractProbeTimeoutSec = [Math]::Min($authoritativeHealthProbeTimeoutSec, [int]$remainingSeconds)" in authoritative
    assert "Get-ContractSnapshot -BaseUrl $BaseUrl -ProbeTimeoutSec $contractProbeTimeoutSec" in authoritative


def test_authoritative_launcher_does_not_enable_projection_worker():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert 'BHM_PROJECTION_WORKER_ENABLED = "true"' not in text


def test_authoritative_launcher_stops_projection_sidecar_on_stop_and_restart():
    text = LAUNCHER.read_text(encoding="utf-8")

    assert text.index("if ($StopOnly)") < text.index("Stop-ProjectionSidecar", text.index("if ($StopOnly)"))
    assert text.index("if ($ForceRestart") < text.index("Stop-ProjectionSidecar", text.index("if ($ForceRestart"))


def test_authoritative_launcher_starts_sidecar_when_qdrant_recovery_is_skipped():
    text = LAUNCHER.read_text(encoding="utf-8")

    assert "function Start-ProjectionSidecar" in text
    initial_ready_block = text[
        text.index("if ($initial.authoritative)") : text.index("if ($NoWait)")
    ]
    assert "Start-ProjectionSidecar" in initial_ready_block
    no_wait_block = text[text.index("if ($NoWait)") : text.index("$result = Wait-Authoritative")]
    assert "Start-ProjectionSidecar" in no_wait_block
    sidecar_start = text[text.index("function Start-ProjectionSidecar") : text.index("function Get-BhmCallerToken")]
    assert "Start-Process -FilePath 'powershell.exe'" in sidecar_start
    assert "'-Action', 'Start', '-NoWait'" in sidecar_start
    assert "& powershell.exe" not in sidecar_start
    final_start = text[text.index("# `-SkipProjectionRecovery`") :]
    assert "Start-ProjectionSidecar" in final_start


def test_service_authoritative_switch_sets_complete_writer_gate_contract():
    text = SERVICE.read_text(encoding="utf-8")
    authoritative_block = text[
        text.index("if ($Authoritative)") : text.index("# Semantic fusion is never implicit")
    ]

    for marker in (
        'BHM_MEMORY_STORE_MODE = "sqlite-authoritative"',
        'BHM_QDRANT_REQUIRED_FOR_CORE = "false"',
        'BHM_FALLBACK_MODE = "explicit"',
        'BHM_PROJECTION_WORKER_ENABLED = "false"',
        'BHM_MEMORY_STORE_PARITY_CONFIRMED = "true"',
        'BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED = "true"',
    ):
        assert marker in authoritative_block


def test_service_inherits_operator_capability_from_user_scope_without_logging_it():
    text = SERVICE.read_text(encoding="utf-8")
    assert "BHM_ADMIN_CAPABILITY" in text
    assert "GetEnvironmentVariable('BHM_ADMIN_CAPABILITY', 'User')" in text
    assert "$env:BHM_ADMIN_CAPABILITY = $userAdminCapability" in text
    assert "Write-Host" not in text[text.index("BHM_ADMIN_CAPABILITY") : text.index("$apiParts")]


def test_service_imports_only_explicit_non_secret_governed_feature_configuration_from_user_scope():
    text = SERVICE.read_text(encoding="utf-8")
    feature_block = text[
        text.index("function Import-ExplicitOperatorFeatureConfiguration") : text.index(
            "# The destructive/operator capability"
        )
    ]

    for name in (
        "BHM_GOVERNED_CONSOLIDATION_ENABLED",
        "BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED",
        "BHM_GOVERNED_SEMANTIC_EDITOR_ENABLED",
        "BHM_GOVERNED_SEMANTIC_EDITOR_BASE_URL",
        "BHM_GOVERNED_SEMANTIC_EDITOR_MODEL",
        "BHM_GOVERNED_SEMANTIC_EDITOR_TIMEOUT_SECONDS",
        "BHM_GOVERNED_SEMANTIC_EDITOR_MAX_TOKENS",
    ):
        assert name in feature_block

    assert "BHM_CALLER_TOKEN" not in feature_block
    assert "BHM_ADMIN_CAPABILITY" not in feature_block
    assert "Write-Host" not in feature_block
