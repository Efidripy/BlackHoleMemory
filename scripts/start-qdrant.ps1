param(
    [ValidateRange(5, 300)][int]$TimeoutSec = 120,
    [ValidateRange(1, 30)][int]$HealthProbeTimeoutSec = 5,
    [ValidateRange(1, 10)][int]$PollSeconds = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
$composeFile = Join-Path $repoRoot "infra\qdrant\docker-compose.yml"
$qdrantHealthUrl = Get-BhmRuntimeEndpoint -Name 'qdrant_http' -RepoRoot $repoRoot -Path 'healthz'
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
$dockerCommandTimeoutSec = 20
$dockerExecutable = $null
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if ($null -ne $dockerCommand) {
    $dockerExecutable = $dockerCommand.Source
} else {
    foreach ($candidate in @(
        (Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'),
        (Join-Path $env:LOCALAPPDATA 'Docker\Docker\resources\bin\docker.exe')
    )) {
        if (Test-Path -LiteralPath $candidate) {
            $dockerExecutable = $candidate
            break
        }
    }
}
if ([string]::IsNullOrWhiteSpace($dockerExecutable)) {
    throw 'Docker executable was not found on PATH or in the Docker Desktop installation paths.'
}

function Invoke-DockerBounded {
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 20
    )
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $dockerExecutable
    $psi.Arguments = ($Arguments | ForEach-Object {
        $value = [string]$_
        if ($value -match '[\s"]') { '"' + $value.Replace('"', '\\"') + '"' } else { $value }
    }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $psi
    if (-not $process.Start()) { throw 'Docker process could not be started.' }
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try { $process.Kill($true) } catch { }
        return [pscustomobject]@{ TimedOut = $true; ExitCode = $null; Output = ''; Error = "docker command timed out after $TimeoutSeconds seconds" }
    }
    [pscustomobject]@{
        TimedOut = $false
        ExitCode = $process.ExitCode
        Output = $process.StandardOutput.ReadToEnd()
        Error = $process.StandardError.ReadToEnd()
    }
}

function Test-DockerEngineReady {
    $result = Invoke-DockerBounded -Arguments @('info', '--format', '{{.ServerVersion}}') -TimeoutSeconds 3
    return (-not $result.TimedOut) -and $result.ExitCode -eq 0
}

function Start-DockerDesktopBounded {
    if (Test-DockerEngineReady) { return }
    $desktopPath = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path -LiteralPath $desktopPath)) {
        throw "Docker engine is unavailable and Docker Desktop was not found."
    }
    Start-Process -FilePath $desktopPath -WindowStyle Hidden | Out-Null
    do {
        if (Test-DockerEngineReady) { return }
        Start-Sleep -Seconds $PollSeconds
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Docker engine did not become ready within $TimeoutSec seconds."
}

Start-DockerDesktopBounded

# Docker Desktop writes normal progress lines to stderr on Windows. Capture
# both streams explicitly and keep compose recovery bounded as well.
$composeResult = Invoke-DockerBounded -Arguments @('compose', '-f', $composeFile, 'up', '-d') -TimeoutSeconds $dockerCommandTimeoutSec
if ($composeResult.TimedOut -or $composeResult.ExitCode -ne 0) {
    $composeDetail = (@($composeResult.Output, $composeResult.Error) | Where-Object { $_ }) -join "; "
    if ($composeResult.TimedOut) { $composeDetail = $composeResult.Error }
    $composeExitCode = if ($null -eq $composeResult.ExitCode) { 'timeout' } else { $composeResult.ExitCode }
    throw "Qdrant docker compose startup failed with exit code $composeExitCode`: $composeDetail"
}
$lastError = "Qdrant HTTP readiness has not completed"
do {
    try {
        # This is a per-probe HTTP bound, distinct from the total startup
        # deadline and the bounded Docker CLI process lifetime.
        $response = Invoke-WebRequest -UseBasicParsing -Uri $qdrantHealthUrl -TimeoutSec $HealthProbeTimeoutSec
        if ([int]$response.StatusCode -eq 200) {
            $response.Content
            exit 0
        }
        $lastError = "HTTP $([int]$response.StatusCode)"
    } catch {
        $lastError = $_.Exception.Message
    }
    if ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Seconds $PollSeconds
    }
} while ([DateTime]::UtcNow -lt $deadline)

throw "Qdrant did not become HTTP-ready within $TimeoutSec seconds: $lastError"
