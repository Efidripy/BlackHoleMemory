param(
    [string]$RepoPath = "E:\GitHub\repos\BlackHoleMemory",
    [string]$BaseUrl = '',
    [int]$Iterations = 500,
    [int]$Warmup = 25,
    [int]$PayloadBytes = 4096,
    [switch]$SkipLive,
    [switch]$AsJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\runtime-endpoints.ps1')
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot (Split-Path -Parent $PSScriptRoot) }
$results = [System.Collections.Generic.List[object]]::new()

function Add-Check {
    param([string]$Name, [bool]$Passed, [string]$Detail)
    $results.Add([pscustomobject]@{
        check = $Name
        status = if ($Passed) { "PASS" } else { "FAIL" }
        detail = $Detail
    }) | Out-Null
}

$testPath = Join-Path $RepoPath "tests\integration\test_pure_core_features.py"
$observationBenchmark = Join-Path $RepoPath "scripts\benchmark-observation-store.py"
$hookBenchmark = Join-Path $RepoPath "scripts\benchmark-hook-queue.py"
$observationModule = Join-Path $RepoPath "src\blackholememory\observation_store.py"
$hookModule = Join-Path $RepoPath "src\blackholememory\hook_queue.py"
$appModule = Join-Path $RepoPath "src\blackholememory\app.py"

foreach ($file in @($testPath, $observationBenchmark, $hookBenchmark, $observationModule, $hookModule, $appModule)) {
    Add-Check -Name "file:$([IO.Path]::GetFileName($file))" -Passed (
        Test-Path -LiteralPath $file -PathType Leaf
    ) -Detail $file
}

$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $RepoPath "src"
    $compileOutput = @(& python -m py_compile $observationModule $hookModule $appModule $observationBenchmark $hookBenchmark 2>&1)
    $compileExitCode = $LASTEXITCODE
    Add-Check -Name "python_compile" -Passed ($compileExitCode -eq 0) -Detail $(
        if ($compileExitCode -eq 0) { "P1.9 stores, app and benchmarks compile" }
        else { $compileOutput -join "; " }
    )

    $pytestOutput = @(
        & python -W error -m pytest $testPath -q `
            -o asyncio_default_fixture_loop_scope=function `
            -k "p1_9" 2>&1
    )
    $pytestExitCode = $LASTEXITCODE
    $pytestDetail = if ($pytestOutput.Count) { [string]$pytestOutput[-1] } else { "no pytest output" }
    Add-Check -Name "p1_9_regression_tests" -Passed ($pytestExitCode -eq 0) -Detail $pytestDetail

    if ($Iterations -lt 100 -or $Warmup -lt 0 -or $PayloadBytes -lt 0) {
        throw "Iterations must be >=100; warmup and payload bytes must be non-negative."
    }

    $observationOutput = @(
        & python $observationBenchmark `
            --iterations $Iterations `
            --warmup $Warmup `
            --payload-bytes $PayloadBytes 2>&1
    )
    $observationExitCode = $LASTEXITCODE
    $observation = if ($observationExitCode -eq 0) {
        ($observationOutput -join [Environment]::NewLine) | ConvertFrom-Json
    } else {
        $null
    }
    Add-Check -Name "observation_scale_completed" -Passed (
        $observationExitCode -eq 0 -and
        $null -ne $observation -and
        [int]$observation.measuredInserted -eq $Iterations -and
        [int]$observation.totalInserted -eq ($Iterations + $Warmup)
    ) -Detail $(
        if ($null -ne $observation) {
            "measured=$($observation.measuredInserted); total=$($observation.totalInserted); p95=$($observation.p95Ms) ms"
        } else { $observationOutput -join "; " }
    )
    Add-Check -Name "observation_scale_p95_under_50ms" -Passed (
        $null -ne $observation -and [double]$observation.p95Ms -lt 50.0
    ) -Detail "observation WAL append p95 remains below 50 ms at the P1.9 scale tier"
    Add-Check -Name "observation_scale_integrity" -Passed (
        $null -ne $observation -and
        [string]$observation.store.journalMode -eq "wal" -and
        [string]$observation.store.integrity -eq "ok"
    ) -Detail "observation WAL remains recoverable and quick_check=ok"

    $hookOutput = @(
        & python $hookBenchmark `
            --iterations $Iterations `
            --warmup $Warmup `
            --payload-bytes $PayloadBytes `
            --drain-worker 2>&1
    )
    $hookExitCode = $LASTEXITCODE
    $hook = if ($hookExitCode -eq 0) {
        ($hookOutput -join [Environment]::NewLine) | ConvertFrom-Json
    } else {
        $null
    }
    Add-Check -Name "hook_queue_scale_completed" -Passed (
        $hookExitCode -eq 0 -and
        $null -ne $hook -and
        [int]$hook.inserted -eq ($Iterations + $Warmup) -and
        [int]$hook.queue.counts.completed -eq [int]$hook.inserted -and
        [int]$hook.queue.counts.failed -eq 0
    ) -Detail $(
        if ($null -ne $hook) {
            "inserted=$($hook.inserted); completed=$($hook.queue.counts.completed); failed=$($hook.queue.counts.failed); p95=$($hook.p95Ms) ms"
        } else { $hookOutput -join "; " }
    )
    Add-Check -Name "hook_queue_scale_p95_under_75ms" -Passed (
        $null -ne $hook -and [double]$hook.p95Ms -le 75.0
    ) -Detail "contended hook enqueue p95 remains <=75 ms at the P1.9 scale tier"
    Add-Check -Name "hook_queue_scale_drained" -Passed (
        $null -ne $hook -and
        [int]$hook.queue.pending -eq 0 -and
        [int]$hook.queue.counts.queued -eq 0 -and
        [int]$hook.queue.counts.processing -eq 0
    ) -Detail "every committed hook job drains without queued/processing residue"
    Add-Check -Name "hook_queue_scale_integrity" -Passed (
        $null -ne $hook -and
        [string]$hook.queue.journalMode -eq "wal" -and
        [string]$hook.queue.integrity -eq "ok"
    ) -Detail "hook queue WAL remains recoverable and quick_check=ok"
} finally {
    $env:PYTHONPATH = $previousPythonPath
}

if (-not $SkipLive) {
    $securityValidator = Join-Path $RepoPath "scripts\validate-bhm-observation-security.ps1"
    $securityOutput = @(
        & powershell -NoProfile -ExecutionPolicy Bypass -File $securityValidator -BaseUrl $BaseUrl -AsJson 2>&1
    )
    $securityExitCode = $LASTEXITCODE
    $security = if ($securityExitCode -eq 0) {
        ($securityOutput -join [Environment]::NewLine) | ConvertFrom-Json
    } else {
        $null
    }
    Add-Check -Name "live_secret_ingress_guard" -Passed (
        $securityExitCode -eq 0 -and
        $null -ne $security -and
        [bool]$security.success -and
        @($security.checks | Where-Object {
            $_.check -in @("live_schema_security_fields", "live_content_length_guard") -and
            $_.status -eq "PASS"
        }).Count -eq 2
    ) -Detail $(
        if ($null -ne $security) {
            "security gate pass=$($security.pass); fail=$($security.fail)"
        } else { $securityOutput -join "; " }
    )
}

$passCount = @($results | Where-Object status -eq "PASS").Count
$failCount = @($results | Where-Object status -eq "FAIL").Count
$summary = [pscustomobject]@{
    success = ($failCount -eq 0)
    iterations = $Iterations
    warmup = $Warmup
    payloadBytes = $PayloadBytes
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
