Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')

$serviceUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot
$reportDir = Join-Path $PSScriptRoot "..\.runtime\cutover"
$reportPath = Join-Path $reportDir "cutover-readiness-latest.json"
$surfaceValidator = Join-Path $PSScriptRoot "validate-bhm-mcp-surface.ps1"
$runtimeValidator = Join-Path $PSScriptRoot "validate-bhm-only-runtime.ps1"
$identityValidator = Join-Path $PSScriptRoot "validate-bhm-observation-identity.ps1"
$securityValidator = Join-Path $PSScriptRoot "validate-bhm-observation-security.ps1"
$storeValidator = Join-Path $PSScriptRoot "validate-bhm-observation-store.ps1"
$hookQueueValidator = Join-Path $PSScriptRoot "validate-bhm-hook-queue.ps1"
$retentionValidator = Join-Path $PSScriptRoot "validate-bhm-retention.ps1"
$resilienceValidator = Join-Path $PSScriptRoot "validate-bhm-p1.9-resilience.ps1"

New-Item -ItemType Directory -Force -Path $reportDir | Out-Null

$ready = Invoke-RestMethod -Method Get "$serviceUrl/health/ready"
$cutover = Invoke-RestMethod -Method Get "$serviceUrl/health/cutover"

$surfaceJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $surfaceValidator
$surfaceExitCode = $LASTEXITCODE
$surface = $surfaceJson | ConvertFrom-Json

$runtimeJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $runtimeValidator -BaseUrl $serviceUrl -AsJson
$runtimeExitCode = $LASTEXITCODE
$runtime = $runtimeJson | ConvertFrom-Json

$identityJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $identityValidator -AsJson
$identityExitCode = $LASTEXITCODE
$identity = $identityJson | ConvertFrom-Json

$securityJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $securityValidator -BaseUrl $serviceUrl -AsJson
$securityExitCode = $LASTEXITCODE
$security = $securityJson | ConvertFrom-Json

$storeJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $storeValidator -BaseUrl $serviceUrl -AsJson
$storeExitCode = $LASTEXITCODE
$store = $storeJson | ConvertFrom-Json

$hookQueueJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $hookQueueValidator -BaseUrl $serviceUrl -AsJson
$hookQueueExitCode = $LASTEXITCODE
$hookQueue = $hookQueueJson | ConvertFrom-Json

$retentionJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $retentionValidator -BaseUrl $serviceUrl -AsJson
$retentionExitCode = $LASTEXITCODE
$retention = $retentionJson | ConvertFrom-Json

$resilienceJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $resilienceValidator -BaseUrl $serviceUrl -AsJson
$resilienceExitCode = $LASTEXITCODE
$resilience = $resilienceJson | ConvertFrom-Json

$gate = [pscustomobject]@{
    ready_ok = [bool]$ready.ok
    cutover_ok = [bool]$cutover.ok
    surface_ok = ([bool]$surface.ok -and $surfaceExitCode -eq 0)
    bhm_only_runtime_ok = ([bool]$runtime.ok -and $runtimeExitCode -eq 0)
    observation_identity_ok = ([bool]$identity.success -and $identityExitCode -eq 0)
    observation_security_ok = ([bool]$security.success -and $securityExitCode -eq 0)
    observation_store_ok = ([bool]$store.success -and $storeExitCode -eq 0)
    hook_queue_ok = ([bool]$hookQueue.success -and $hookQueueExitCode -eq 0)
    retention_ok = ([bool]$retention.success -and $retentionExitCode -eq 0)
    p1_9_resilience_ok = ([bool]$resilience.success -and $resilienceExitCode -eq 0)
}
$gate | Add-Member -NotePropertyName overall_ok -NotePropertyValue (
    $gate.ready_ok -and
    $gate.cutover_ok -and
    $gate.surface_ok -and
    $gate.bhm_only_runtime_ok -and
    $gate.observation_identity_ok -and
    $gate.observation_security_ok -and
    $gate.observation_store_ok -and
    $gate.hook_queue_ok -and
    $gate.retention_ok -and
    $gate.p1_9_resilience_ok
)

$report = [pscustomobject]@{
    generated_at = (Get-Date).ToString("o")
    ready = $ready
    cutover = $cutover
    surface_validation = $surface
    bhm_only_runtime = $runtime
    observation_identity = $identity
    observation_security = $security
    observation_store = $store
    hook_queue = $hookQueue
    retention = $retention
    p1_9_resilience = $resilience
    gate = $gate
}

$report | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $reportPath -Encoding utf8
$report | ConvertTo-Json -Depth 20

if (-not $gate.overall_ok) {
    exit 1
}
