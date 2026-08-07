param(
    [switch]$Apply,
    [string]$BhmBaseUrl = '',
    [string]$LogRoot = "E:\GitHub\workspace\runtime\logs\projects\blackholememory\crystallizer",
    [ValidateRange(1, 600)][int]$TimeoutSeconds = 180,
    [ValidateRange(1, 60)][int]$CleanupTimeoutSeconds = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptPath = $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent $scriptPath)
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
if ([string]::IsNullOrWhiteSpace($BhmBaseUrl)) { $BhmBaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot }
$workerPath = Join-Path $repoRoot "scripts\bhm_crystallize_worker.py"
$timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$runDir = Join-Path $LogRoot "run-$timestamp"
$transcriptPath = Join-Path $runDir "transcript.log"
$compileLog = Join-Path $runDir "py-compile.log"
$workerLog = Join-Path $runDir "worker.log"
$summaryPath = Join-Path $runDir "summary.json"

New-Item -ItemType Directory -Force -Path $runDir | Out-Null

function Invoke-LoggedNative {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$LogPath
    )

    $start = Get-Date
    $stdoutPath = Join-Path $runDir "$Name.stdout.tmp"
    $stderrPath = Join-Path $runDir "$Name.stderr.tmp"
    "=== $Name ===" | Tee-Object -FilePath $LogPath | Out-Null
    "cwd: $repoRoot" | Tee-Object -FilePath $LogPath -Append | Out-Null
    "cmd: $FilePath $($Arguments -join ' ')" | Tee-Object -FilePath $LogPath -Append | Out-Null
    "" | Tee-Object -FilePath $LogPath -Append | Out-Null

    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $repoRoot `
        -NoNewWindow `
        -PassThru `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath

    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try {
            $process.Kill()
        } catch {
        }
        if (-not $process.WaitForExit($CleanupTimeoutSeconds * 1000)) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            $process.WaitForExit($CleanupTimeoutSeconds * 1000) | Out-Null
        }
        $exitCode = 124
        "TIMEOUT after $TimeoutSeconds seconds" | Tee-Object -FilePath $LogPath -Append | Out-Null
    }
    else {
        $process.Refresh()
        $exitCode = [int]$process.ExitCode
    }

    if (Test-Path $stdoutPath) {
        "=== stdout ===" | Tee-Object -FilePath $LogPath -Append | Out-Null
        Get-Content -Path $stdoutPath -Raw | Tee-Object -FilePath $LogPath -Append | Out-Null
    }
    if (Test-Path $stderrPath) {
        "=== stderr ===" | Tee-Object -FilePath $LogPath -Append | Out-Null
        $stderrContent = Get-Content -Path $stderrPath -Raw
        $stderrContent | Tee-Object -FilePath $LogPath -Append | Out-Null
        if ($Name -eq "worker_once" -and $stderrContent -match "LLM synthesis failed|Мягкая остановка итерации") {
            $exitCode = 1
        }
    }

    $duration = [math]::Round(((Get-Date) - $start).TotalSeconds, 3)
    "" | Tee-Object -FilePath $LogPath -Append | Out-Null
    "exit_code: $exitCode" | Tee-Object -FilePath $LogPath -Append | Out-Null
    "duration_seconds: $duration" | Tee-Object -FilePath $LogPath -Append | Out-Null

    [pscustomobject]@{
        name = $Name
        exit_code = $exitCode
        duration_seconds = $duration
        log = $LogPath
    }
}

$results = @()
$workerArgs = @(
    "scripts\bhm_crystallize_worker.py",
    "--once",
    "--log-level",
    "DEBUG",
    "--strict-exit",
    "--max-log-chars",
    "350",
    "--max-payload-chars",
    "8000",
    "--timeout",
    "120",
    "--bhm-base-url",
    $BhmBaseUrl
)
if ($Apply) {
    $workerArgs += "--apply"
}
$verifyArgs = @(
    "scripts\verify-bhm-crystallize-worker.py",
    "--run-dir",
    $runDir,
    "--bhm-base-url",
    $BhmBaseUrl
)

Start-Transcript -Path $transcriptPath -Force | Out-Null
try {
    Write-Host "BHM crystallizer test run"
    Write-Host "repo: $repoRoot"
    Write-Host "worker: $workerPath"
    Write-Host "apply: $($Apply.IsPresent)"
    Write-Host "logs: $runDir"

    $results += Invoke-LoggedNative -Name "py_compile" -FilePath "python" -Arguments @("-m", "py_compile", "scripts\bhm_crystallize_worker.py") -LogPath $compileLog
    if ($results[-1].exit_code -ne 0) {
        throw "py_compile failed"
    }

    $results += Invoke-LoggedNative -Name "worker_once" -FilePath "python" -Arguments $workerArgs -LogPath $workerLog
    $results += Invoke-LoggedNative -Name "verify_worker_run" -FilePath "python" -Arguments $verifyArgs -LogPath (Join-Path $runDir "verify.log")
}
finally {
    Stop-Transcript | Out-Null
}

$summary = [pscustomobject]@{
    timestamp = $timestamp
    repo = $repoRoot
    apply = $Apply.IsPresent
    bhm_base_url = $BhmBaseUrl
    run_dir = $runDir
    transcript = $transcriptPath
    results = $results
    ok = -not ($results | Where-Object { $_.exit_code -ne 0 })
}

$summary | ConvertTo-Json -Depth 8 | Set-Content -Path $summaryPath -Encoding utf8
$summary | ConvertTo-Json -Depth 8

if (-not $summary.ok) {
    exit 1
}
