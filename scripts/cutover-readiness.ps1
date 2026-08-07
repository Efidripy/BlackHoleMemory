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

$CutoverReadinessHttpTimeoutSec = 10
$CutoverReadinessMaxResponseBytes = 262144

function Get-BhmCallerHeaders {
    $token = [string][Environment]::GetEnvironmentVariable('BHM_CALLER_TOKEN', 'Process')
    if ([string]::IsNullOrWhiteSpace($token)) {
        $token = [string][Environment]::GetEnvironmentVariable('BHM_CALLER_TOKEN', 'User')
    }
    if ([string]::IsNullOrWhiteSpace($token)) {
        $envPath = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.bhm\.env'
        foreach ($line in Get-Content -LiteralPath $envPath -ErrorAction SilentlyContinue) {
            if ($line -match '^\s*BHM_CALLER_TOKEN\s*=') {
                $token = $line.Split('=', 2)[1].Split('#', 2)[0].Trim().Trim('"').Trim("'")
                break
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace($token) -or $token.Trim().Length -lt 32) {
        throw 'BHM_CALLER_TOKEN is unavailable'
    }
    return @{ Authorization = "Bearer $($token.Trim())"; 'X-BHM-Caller-Surface' = 'cutover-validator' }
}

function Assert-CutoverReadinessUri {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    $parsed = $null
    if (-not [Uri]::TryCreate($Candidate, [UriKind]::Absolute, [ref]$parsed)) {
        throw 'cutover readiness URL is not an absolute URI'
    }
    $allowedHosts = @('127.0.0.1', 'localhost', '::1')
    if ($parsed.Scheme -ne 'http' -or $allowedHosts -notcontains $parsed.Host.ToLowerInvariant()) {
        throw 'cutover readiness requires an HTTP loopback endpoint'
    }
    if (-not [string]::IsNullOrWhiteSpace($parsed.UserInfo)) {
        throw 'cutover readiness URL must not contain userinfo'
    }
    if (-not [string]::IsNullOrWhiteSpace($parsed.Fragment)) {
        throw 'cutover readiness URL must not contain a fragment'
    }
    return $parsed
}

function Invoke-CutoverReadinessJson {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [hashtable]$Headers = @{}
    )

    $parsed = Assert-CutoverReadinessUri -Candidate $Uri
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $handler.UseProxy = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($CutoverReadinessHttpTimeoutSec)
    $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $parsed)
    $response = $null
    try {
        foreach ($key in $Headers.Keys) {
            $request.Headers.TryAddWithoutValidation([string]$key, [string]$Headers[$key]) | Out-Null
        }
        $response = $client.SendAsync(
            $request,
            [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw "cutover readiness returned HTTP $([int]$response.StatusCode)"
        }
        $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        if ($bytes.Length -gt $CutoverReadinessMaxResponseBytes) {
            throw "cutover readiness response exceeded bounded limit $CutoverReadinessMaxResponseBytes bytes"
        }
        return ([Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json)
    }
    finally {
        if ($null -ne $response) { $response.Dispose() }
        $request.Dispose()
        $client.Dispose()
        $handler.Dispose()
    }
}

New-Item -ItemType Directory -Force -Path $reportDir | Out-Null

$headers = Get-BhmCallerHeaders
$ready = Invoke-CutoverReadinessJson -Uri "$($serviceUrl.TrimEnd('/'))/health/ready"
$cutover = Invoke-CutoverReadinessJson -Uri "$($serviceUrl.TrimEnd('/'))/health/cutover" -Headers $headers

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
