param(
    [string]$BaseUrl = '',
    [string]$WorkspaceRoot = "E:\GitHub",
    [string]$RepoPath = "E:\GitHub\repos\BlackHoleMemory",
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot }
Add-Type -AssemblyName System.Net.Http

function Get-BhmCallerHeaders {
    $token = [string]$env:BHM_CALLER_TOKEN
    if ([string]::IsNullOrWhiteSpace($token) -or $token.Trim().Length -lt 32) {
        $token = [string][Environment]::GetEnvironmentVariable('BHM_CALLER_TOKEN', 'User')
    }
    $token = $token.Trim()
    if ($token.Length -lt 32) {
        throw 'BHM_CALLER_TOKEN is unavailable'
    }
    return @{ Authorization = "Bearer $token" }
}

$results = [System.Collections.Generic.List[object]]::new()
$senderScript = Join-Path $WorkspaceRoot "workspace\control\scripts\shared\send-bhm-observe.ps1"
$hookScript = Join-Path $WorkspaceRoot "workspace\control\scripts\shared\invoke-bhm-codex-hook.ps1"
$securityModule = Join-Path $RepoPath "src\blackholememory\observation_security.py"
$testPath = Join-Path $RepoPath "tests\integration\test_pure_core_features.py"

function Add-Check {
    param([string]$Name, [bool]$Passed, [string]$Detail)
    $results.Add([pscustomobject]@{
        check = $Name
        status = if ($Passed) { "PASS" } else { "FAIL" }
        detail = $Detail
    }) | Out-Null
}

foreach ($file in @($senderScript, $hookScript, $securityModule, $testPath)) {
    Add-Check -Name "file:$([IO.Path]::GetFileName($file))" -Passed (Test-Path -LiteralPath $file) -Detail $file
}

foreach ($file in @($senderScript, $hookScript)) {
    if (-not (Test-Path -LiteralPath $file)) { continue }
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$parseErrors) | Out-Null
    Add-Check -Name "syntax:$([IO.Path]::GetFileName($file))" -Passed (@($parseErrors).Count -eq 0) -Detail $(
        if (@($parseErrors).Count -eq 0) { "PowerShell parser clean" } else { (@($parseErrors | ForEach-Object Message) -join '; ') }
    )
}

$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $RepoPath "src"
    $pytestOutput = @(
        & python -W error -m pytest $testPath -q `
            -o asyncio_default_fixture_loop_scope=function `
            -k "observation_security or secret_text_redaction or observe_endpoint or hook_observation or compact_hook_sanitizes or hook_idle_durable_enqueue or memory_redaction or galaxy_observation_rollup" 2>&1
    )
    $pytestExitCode = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
$pytestDetail = if ($pytestOutput.Count) { [string]$pytestOutput[-1] } else { "no pytest output" }
Add-Check -Name "security_regression_tests" -Passed ($pytestExitCode -eq 0) -Detail $pytestDetail

$tempDataFile = Join-Path $env:TEMP ("bhm-observation-security-{0}.json" -f [guid]::NewGuid().ToString('N'))
$oversizeDataFile = Join-Path $env:TEMP ("bhm-observation-security-oversize-{0}.json" -f [guid]::NewGuid().ToString('N'))
try {
    [IO.File]::WriteAllText(
        $tempDataFile,
        '{"safe":"value","token_count":7}',
        [Text.UTF8Encoding]::new($false)
    )
    $dryRunOutput = @(
        & powershell -NoProfile -ExecutionPolicy Bypass -File $senderScript `
            -HookType "workspace_security_gate" `
            -SessionId "security-gate-session" `
            -Project "blackholememory" `
            -Cwd $RepoPath `
            -DataFile $tempDataFile `
            -DryRun 2>&1
    )
    $dryRunExitCode = $LASTEXITCODE
    $dryRun = if ($dryRunExitCode -eq 0) { ($dryRunOutput -join [Environment]::NewLine) | ConvertFrom-Json } else { $null }
    Add-Check -Name "writer_reports_payload_budget" -Passed (
        $dryRunExitCode -eq 0 -and
        $dryRun.payloadBytes -gt 0 -and
        $dryRun.payloadBytes -le $dryRun.maxPayloadBytes
    ) -Detail "workspace sender emits bounded payload byte evidence"

    $oversizeJson = '{"blob":"' + ('x' * (270 * 1024)) + '"}'
    [IO.File]::WriteAllText($oversizeDataFile, $oversizeJson, [Text.UTF8Encoding]::new($false))
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $oversizeOutput = @(
            & powershell -NoProfile -ExecutionPolicy Bypass -File $senderScript `
                -HookType "workspace_security_gate_oversize" `
                -SessionId "security-gate-session" `
                -Project "blackholememory" `
                -Cwd $RepoPath `
                -DataFile $oversizeDataFile `
                -DryRun 2>&1
        )
        $oversizeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    Add-Check -Name "writer_rejects_oversize" -Passed (
        $oversizeExitCode -ne 0 -and
        (($oversizeOutput -join " ") -match "exceeds input limit")
    ) -Detail "workspace sender fails before network send"
} finally {
    Remove-Item -LiteralPath $tempDataFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $oversizeDataFile -Force -ErrorAction SilentlyContinue
}

try {
    $openapi = Invoke-RestMethod -Method Get -Uri "$BaseUrl/openapi.json" -TimeoutSec 15
    $observeSchema = $openapi.components.schemas.ObservationIngressV1
    $compactSchema = $openapi.components.schemas.BhmHookCompactRequest
    Add-Check -Name "live_schema_security_fields" -Passed (
        $null -ne $observeSchema.properties.payloadState -and
        $null -ne $observeSchema.properties.sensitivity -and
        $null -ne $compactSchema.properties.payloadState -and
        $null -ne $compactSchema.properties.sensitivity
    ) -Detail "live OpenAPI exposes payload state and sensitivity"
} catch {
    Add-Check -Name "live_schema_security_fields" -Passed $false -Detail $_.Exception.Message
}

$client = [System.Net.Http.HttpClient]::new()
try {
    $client.Timeout = [TimeSpan]::FromSeconds(20)
    $callerHeaders = Get-BhmCallerHeaders
    $client.DefaultRequestHeaders.TryAddWithoutValidation('Authorization', [string]$callerHeaders.Authorization) | Out-Null
    $oversizedRequest = [ordered]@{
        hookType = "workspace_security_gate_oversize"
        sessionId = "security-gate-session"
        project = "blackholememory"
        cwd = $RepoPath
        data = @{ blob = ('x' * (260 * 1024)) }
    }
    $json = $oversizedRequest | ConvertTo-Json -Depth 10 -Compress
    $content = [System.Net.Http.StringContent]::new(
        $json,
        [System.Text.UTF8Encoding]::new($false),
        "application/json"
    )
    $response = $client.PostAsync("$BaseUrl/bhm/observe", $content).GetAwaiter().GetResult()
    Add-Check -Name "live_content_length_guard" -Passed ([int]$response.StatusCode -eq 413) -Detail "live ingress rejects oversized body with HTTP 413"
    $response.Dispose()
} catch {
    Add-Check -Name "live_content_length_guard" -Passed $false -Detail $_.Exception.Message
} finally {
    $client.Dispose()
}

$passCount = @($results | Where-Object status -eq 'PASS').Count
$failCount = @($results | Where-Object status -eq 'FAIL').Count
$summary = [pscustomobject]@{
    success = ($failCount -eq 0)
    pass = $passCount
    fail = $failCount
    checks = $results
}

if ($AsJson) {
    $summary | ConvertTo-Json -Depth 20
} else {
    foreach ($result in $results) {
        Write-Host "[$($result.status)] $($result.check): $($result.detail)"
    }
    Write-Host "Summary: PASS=$passCount FAIL=$failCount"
}

if ($failCount -gt 0) {
    exit 1
}
