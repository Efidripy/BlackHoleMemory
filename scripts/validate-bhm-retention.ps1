param(
    [string]$BaseUrl = '',
    [string]$RepoPath = "E:\GitHub\repos\BlackHoleMemory",
    [switch]$SkipLive,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot }
$results = [System.Collections.Generic.List[object]]::new()

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

function Add-Check {
    param([string]$Name, [bool]$Passed, [string]$Detail)
    $results.Add([pscustomobject]@{
        check = $Name
        status = if ($Passed) { "PASS" } else { "FAIL" }
        detail = $Detail
    }) | Out-Null
}

$retentionModule = Join-Path $RepoPath "src\blackholememory\retention.py"
$observationModule = Join-Path $RepoPath "src\blackholememory\observation_store.py"
$hookQueueModule = Join-Path $RepoPath "src\blackholememory\hook_queue.py"
$maintenanceScript = Join-Path $RepoPath "scripts\bhm_retention_maintenance.py"
$policyPath = Join-Path $RepoPath "config\retention-policy.json"
$testPath = Join-Path $RepoPath "tests\integration\test_pure_core_features.py"

foreach ($file in @($retentionModule, $observationModule, $hookQueueModule, $maintenanceScript, $policyPath, $testPath)) {
    Add-Check -Name "file:$([IO.Path]::GetFileName($file))" -Passed (Test-Path -LiteralPath $file) -Detail $file
}

$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $RepoPath "src"
    $compileOutput = @(
        & python -m py_compile $retentionModule $observationModule $hookQueueModule $maintenanceScript 2>&1
    )
    $compileExitCode = $LASTEXITCODE
    Add-Check -Name "python_compile" -Passed ($compileExitCode -eq 0) -Detail $(
        if ($compileExitCode -eq 0) { "retention policy, stores and maintenance CLI compile" } else { $compileOutput -join '; ' }
    )

    $policyOutput = @(
        & python -c "from blackholememory.retention import load_retention_policy; p=load_retention_policy(r'$policyPath'); print(p.schema_version, len(p.observation_rules), len(p.hook_job_rules), p.sha256)" 2>&1
    )
    $policyExitCode = $LASTEXITCODE
    Add-Check -Name "policy_contract" -Passed (
        $policyExitCode -eq 0 -and ($policyOutput -join ' ') -match '^1\.0 10 5 [0-9a-f]{64}$'
    ) -Detail ($policyOutput -join ' ')

    $pytestOutput = @(
        & python -W error -m pytest $testPath -q `
            -o asyncio_default_fixture_loop_scope=function `
            -k "retention or observation_store_expiration or hook_queue_terminal_expiration or sqlite_retention_schema" 2>&1
    )
    $pytestExitCode = $LASTEXITCODE
    $pytestDetail = if ($pytestOutput.Count) { [string]$pytestOutput[-1] } else { "no pytest output" }
    Add-Check -Name "retention_regression_tests" -Passed ($pytestExitCode -eq 0) -Detail $pytestDetail
} finally {
    $env:PYTHONPATH = $previousPythonPath
}

if (-not $SkipLive) {
    try {
        $callerHeaders = Get-BhmCallerHeaders
        $observationStatusBefore = Invoke-RestMethod -Method Get -Uri "$BaseUrl/bhm/observations/store/status?integrity=true" -Headers $callerHeaders -TimeoutSec 20
        $queueStatusBefore = Invoke-RestMethod -Method Get -Uri "$BaseUrl/bhm/hooks/queue/status?integrity=true" -Headers $callerHeaders -TimeoutSec 20
        $asOf = [DateTime]::UtcNow.ToString("o")
        $rules = "synthetic-hook,synthetic-source,synthetic-project,synthetic-hook-job,explicit-purge"
        $dryRunOutput = @(
& python $maintenanceScript --runtime-dir (Join-Path $RepoPath ".runtime\live-memory") `
                --policy $policyPath --rules $rules --as-of $asOf 2>&1
        )
        $dryRunExitCode = $LASTEXITCODE
        $dryRun = if ($dryRunExitCode -eq 0) {
            ($dryRunOutput -join [Environment]::NewLine) | ConvertFrom-Json
        } else {
            $null
        }
        Add-Check -Name "live_dry_run" -Passed (
            $dryRunExitCode -eq 0 -and
            $null -ne $dryRun -and
            [bool]$dryRun.success -and
            [string]$dryRun.mode -eq "dry-run" -and
            [string]$dryRun.plan.planDigest -match '^[0-9a-f]{64}$'
        ) -Detail $(
            if ($null -ne $dryRun) {
                "observations=$($dryRun.plan.observations.expireCount); hookJobs=$($dryRun.plan.hookJobs.expireCount); digest=$($dryRun.plan.planDigest)"
            } else {
                $dryRunOutput -join '; '
            }
        )

        $observationStatusAfter = Invoke-RestMethod -Method Get -Uri "$BaseUrl/bhm/observations/store/status?integrity=true" -Headers $callerHeaders -TimeoutSec 20
        $queueStatusAfter = Invoke-RestMethod -Method Get -Uri "$BaseUrl/bhm/hooks/queue/status?integrity=true" -Headers $callerHeaders -TimeoutSec 20
        Add-Check -Name "live_dry_run_is_read_only" -Passed (
            [int]$observationStatusAfter.total -eq [int]$observationStatusBefore.total -and
            [int]$observationStatusAfter.tombstones -eq [int]$observationStatusBefore.tombstones -and
            [int]$queueStatusAfter.pending -eq [int]$queueStatusBefore.pending -and
            [int]$queueStatusAfter.tombstonesTotal -eq [int]$queueStatusBefore.tombstonesTotal
        ) -Detail "dry-run did not mutate observation or hook job counts"
        Add-Check -Name "live_schema_integrity" -Passed (
            [int]$observationStatusAfter.schemaVersion -eq 2 -and
            [string]$observationStatusAfter.integrity -eq "ok" -and
            [int]$queueStatusAfter.schemaVersion -eq 2 -and
            [string]$queueStatusAfter.integrity -eq "ok"
        ) -Detail "observation and hook queue schema v2 use WAL with quick_check=ok"

        $statusEndpoint = Invoke-RestMethod -Method Get -Uri "$BaseUrl/bhm/retention/status" -Headers $callerHeaders -TimeoutSec 20
        Add-Check -Name "live_status_endpoint" -Passed (
            [bool]$statusEndpoint.success -and
            [string]$statusEndpoint.mode -eq "dry-run" -and
            [string]$statusEndpoint.plan.planDigest -match '^[0-9a-f]{64}$'
        ) -Detail "read-only retention status is available to Codex and operators"
    } catch {
        Add-Check -Name "live_dry_run" -Passed $false -Detail $_.Exception.Message
        Add-Check -Name "live_dry_run_is_read_only" -Passed $false -Detail $_.Exception.Message
        Add-Check -Name "live_schema_integrity" -Passed $false -Detail $_.Exception.Message
        Add-Check -Name "live_status_endpoint" -Passed $false -Detail $_.Exception.Message
    }

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
