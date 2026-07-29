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

$storeModule = Join-Path $RepoPath "src\blackholememory\observation_store.py"
$benchmarkScript = Join-Path $RepoPath "scripts\benchmark-observation-store.py"
$workerScript = Join-Path $RepoPath "scripts\bhm_crystallize_worker.py"
$testPath = Join-Path $RepoPath "tests\integration\test_pure_core_features.py"

foreach ($file in @(
    $storeModule,
    $benchmarkScript,
    $workerScript,
    $testPath
)) {
    Add-Check -Name "file:$([IO.Path]::GetFileName($file))" -Passed (Test-Path -LiteralPath $file) -Detail $file
}

$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $RepoPath "src"
    $compileOutput = @(
        & python -m py_compile `
            $storeModule `
            (Join-Path $RepoPath "src\blackholememory\app.py") `
            (Join-Path $RepoPath "src\blackholememory\galaxy.py") `
            $workerScript `
            $benchmarkScript 2>&1
    )
    $compileExitCode = $LASTEXITCODE
    Add-Check -Name "python_compile" -Passed ($compileExitCode -eq 0) -Detail $(
        if ($compileExitCode -eq 0) { "observation store and consumers compile" } else { $compileOutput -join '; ' }
    )

    $pytestOutput = @(
        & python -W error -m pytest $testPath -q `
            -o asyncio_default_fixture_loop_scope=function `
            -k "observation_store or app_observation_append" 2>&1
    )
    $pytestExitCode = $LASTEXITCODE
    $pytestDetail = if ($pytestOutput.Count) { [string]$pytestOutput[-1] } else { "no pytest output" }
    Add-Check -Name "observation_store_regression_tests" -Passed ($pytestExitCode -eq 0) -Detail $pytestDetail

    $benchmarkOutput = @(
        & python $benchmarkScript --iterations 200 --warmup 10 --payload-bytes 4096 2>&1
    )
    $benchmarkExitCode = $LASTEXITCODE
    $benchmark = if ($benchmarkExitCode -eq 0) {
        ($benchmarkOutput -join [Environment]::NewLine) | ConvertFrom-Json
    } else {
        $null
    }
    Add-Check -Name "benchmark_completed" -Passed ($benchmarkExitCode -eq 0 -and $null -ne $benchmark) -Detail $(
        if ($null -ne $benchmark) { "p95=$($benchmark.p95Ms) ms; max=$($benchmark.maxMs) ms" } else { $benchmarkOutput -join '; ' }
    )
    Add-Check -Name "benchmark_p95_under_50ms" -Passed (
        $null -ne $benchmark -and [double]$benchmark.p95Ms -lt 50.0
    ) -Detail "direct SQLite WAL append p95 must remain below 50 ms"
    Add-Check -Name "benchmark_committed_all_events" -Passed (
        $null -ne $benchmark -and
        [int]$benchmark.measuredInserted -eq [int]$benchmark.iterations -and
        [int]$benchmark.totalInserted -eq ([int]$benchmark.iterations + [int]$benchmark.warmup)
    ) -Detail "all warmup and measured events committed exactly once"
    Add-Check -Name "benchmark_wal_integrity" -Passed (
        $null -ne $benchmark -and
        [string]$benchmark.store.journalMode -eq "wal" -and
        [string]$benchmark.store.integrity -eq "ok"
    ) -Detail "temporary store uses WAL and quick_check=ok"
} finally {
    $env:PYTHONPATH = $previousPythonPath
}

if (-not $SkipLive) {
    try {
        $callerHeaders = Get-BhmCallerHeaders
        $live = Invoke-RestMethod -Method Get -Uri "$BaseUrl/bhm/observations/store/status?integrity=true" -Headers $callerHeaders -TimeoutSec 20
        Add-Check -Name "live_store_status" -Passed (
            [string]$live.journalMode -eq "wal" -and
            [int]$live.schemaVersion -eq 2 -and
            [string]$live.integrity -eq "ok"
        ) -Detail "live SQLite WAL status is green"
    } catch {
        Add-Check -Name "live_store_status" -Passed $false -Detail $_.Exception.Message
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
