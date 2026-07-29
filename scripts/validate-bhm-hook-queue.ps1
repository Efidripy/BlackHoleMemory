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

$queueModule = Join-Path $RepoPath "src\blackholememory\hook_queue.py"
$appModule = Join-Path $RepoPath "src\blackholememory\app.py"
$benchmarkScript = Join-Path $RepoPath "scripts\benchmark-hook-queue.py"
$testPath = Join-Path $RepoPath "tests\integration\test_pure_core_features.py"

foreach ($file in @($queueModule, $appModule, $benchmarkScript, $testPath)) {
    Add-Check -Name "file:$([IO.Path]::GetFileName($file))" -Passed (Test-Path -LiteralPath $file) -Detail $file
}

$appSource = Get-Content -LiteralPath $appModule -Raw -Encoding UTF8
Add-Check -Name "no_unbounded_idle_dispatch" -Passed (
    $appSource -notmatch '_dispatch_idle_reflection_pipeline' -and
    $appSource -notmatch 'bhm-idle-reflex-'
) -Detail "idle hook no longer spawns one daemon thread per request"

$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $RepoPath "src"
    $compileOutput = @(
        & python -m py_compile $queueModule $appModule $benchmarkScript 2>&1
    )
    $compileExitCode = $LASTEXITCODE
    Add-Check -Name "python_compile" -Passed ($compileExitCode -eq 0) -Detail $(
        if ($compileExitCode -eq 0) { "hook queue, app and benchmark compile" } else { $compileOutput -join '; ' }
    )

    $pytestOutput = @(
        & python -W error -m pytest $testPath -q `
            -o asyncio_default_fixture_loop_scope=function `
            -k "hook_queue or hook_idle_durable_enqueue or hook_compact or compact_hook_sanitizes_transit_before_durable_enqueue" 2>&1
    )
    $pytestExitCode = $LASTEXITCODE
    $pytestDetail = if ($pytestOutput.Count) { [string]$pytestOutput[-1] } else { "no pytest output" }
    Add-Check -Name "hook_queue_regression_tests" -Passed ($pytestExitCode -eq 0) -Detail $pytestDetail

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
    Add-Check -Name "enqueue_p95_under_50ms" -Passed (
        $null -ne $benchmark -and [double]$benchmark.p95Ms -lt 50.0
    ) -Detail "durable SQLite WAL enqueue p95 must remain below 50 ms"
    Add-Check -Name "benchmark_committed_all_jobs" -Passed (
        $null -ne $benchmark -and
        [int]$benchmark.inserted -eq ([int]$benchmark.iterations + [int]$benchmark.warmup) -and
        [int]$benchmark.queue.pending -eq [int]$benchmark.inserted
    ) -Detail "all benchmark jobs committed exactly once and remain claimable"
    Add-Check -Name "benchmark_wal_integrity" -Passed (
        $null -ne $benchmark -and
        [string]$benchmark.queue.journalMode -eq "wal" -and
        [string]$benchmark.queue.integrity -eq "ok"
    ) -Detail "temporary queue uses WAL and quick_check=ok"

    $contendedOutput = @(
        & python $benchmarkScript --iterations 200 --warmup 10 --payload-bytes 4096 --drain-worker 2>&1
    )
    $contendedExitCode = $LASTEXITCODE
    $contended = if ($contendedExitCode -eq 0) {
        ($contendedOutput -join [Environment]::NewLine) | ConvertFrom-Json
    } else {
        $null
    }
    Add-Check -Name "contended_benchmark_completed" -Passed (
        $contendedExitCode -eq 0 -and $null -ne $contended
    ) -Detail $(
        if ($null -ne $contended) { "p95=$($contended.p95Ms) ms; max=$($contended.maxMs) ms" } else { $contendedOutput -join '; ' }
    )
    Add-Check -Name "contended_enqueue_p95_under_50ms" -Passed (
        $null -ne $contended -and [double]$contended.p95Ms -lt 50.0
    ) -Detail "enqueue p95 remains below 50 ms while a fixed worker claims and completes jobs"
    Add-Check -Name "contended_worker_drained" -Passed (
        $null -ne $contended -and
        [int]$contended.queue.pending -eq 0 -and
        [int]$contended.queue.counts.completed -eq [int]$contended.inserted
    ) -Detail "contended benchmark drains every committed job exactly once"
} finally {
    $env:PYTHONPATH = $previousPythonPath
}

if (-not $SkipLive) {
    try {
        $callerHeaders = Get-BhmCallerHeaders
        $live = Invoke-RestMethod -Method Get -Uri "$BaseUrl/bhm/hooks/queue/status?integrity=true" -Headers $callerHeaders -TimeoutSec 20
        Add-Check -Name "live_queue_status" -Passed (
            [string]$live.journalMode -eq "wal" -and
            [int]$live.schemaVersion -eq 2 -and
            [string]$live.integrity -eq "ok" -and
            [bool]$live.accepting -and
            [int]$live.workerTasksConfigured -ge 2 -and
            [int]$live.workerTasks -eq [int]$live.workerTasksConfigured -and
            [int]$live.workerTasksStopped -eq 0
        ) -Detail "live queue is accepting with fixed workers and quick_check=ok"
    } catch {
        Add-Check -Name "live_queue_status" -Passed $false -Detail $_.Exception.Message
    }

    $eventId = "obs_hook_queue_gate_$([guid]::NewGuid().ToString('N'))"
    $payload = [ordered]@{
        schemaVersion = "1.0"
        eventId = $eventId
        hookType = "workspace_queue_gate_compact"
        sessionId = "session-hook-queue-gate"
        correlationId = "task-hook-queue-gate"
        project = "blackholememory"
        cwd = $RepoPath
        source = "hook-queue-gate"
        payloadState = "raw"
        data = @{}
    }
    try {
        $callerHeaders = Get-BhmCallerHeaders
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $accepted = Invoke-RestMethod -Method Post -Uri "$BaseUrl/bhm/hooks/compact" `
            -Headers $callerHeaders -ContentType "application/json" -Body ($payload | ConvertTo-Json -Depth 10 -Compress) -TimeoutSec 20
        $watch.Stop()
        Add-Check -Name "live_durable_enqueue" -Passed (
            [bool]$accepted.accepted -and
            [string]$accepted.durability -eq "sqlite-wal" -and
            [string]$accepted.observation.eventId -eq $eventId -and
            $watch.Elapsed.TotalMilliseconds -lt 5000
        ) -Detail "live durable functionality accepted in $([Math]::Round($watch.Elapsed.TotalMilliseconds, 3)) ms; p95 is enforced by dedicated benchmarks"

        $job = $null
        for ($index = 0; $index -lt 100; $index++) {
            $jobResponse = Invoke-RestMethod -Method Get -Uri "$BaseUrl/bhm/hooks/jobs/$($accepted.job.id)" -Headers $callerHeaders -TimeoutSec 10
            $job = $jobResponse.job
            if ([string]$job.status -in @("completed", "failed")) { break }
            Start-Sleep -Milliseconds 100
        }
        Add-Check -Name "live_worker_completion" -Passed (
            $null -ne $job -and
            [string]$job.status -eq "completed" -and
            [string]$job.result.observation.id -eq $eventId
        ) -Detail "fixed compact worker completed the durable job"
    } catch {
        Add-Check -Name "live_durable_enqueue" -Passed $false -Detail $_.Exception.Message
        Add-Check -Name "live_worker_completion" -Passed $false -Detail $_.Exception.Message
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
