param(
    [ValidateRange(5, 300)][int]$TimeoutSec = 120,
    [ValidateRange(1, 10)][int]$PollSeconds = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
$composeFile = Join-Path $repoRoot "infra\qdrant\docker-compose.yml"
$qdrantHealthUrl = Get-BhmRuntimeEndpoint -Name 'qdrant_http' -RepoRoot $repoRoot -Path 'healthz'
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)

function Test-DockerEngineReady {
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $null = & docker info --format '{{.ServerVersion}}' 2>$null
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $savedPreference
    }
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

# Docker Desktop writes normal progress lines to stderr on Windows. Under
# ErrorActionPreference=Stop, invoking it directly can turn a successful
# compose command into a terminating PowerShell error. Capture the native exit
# code explicitly so only a real non-zero result blocks API startup.
$savedErrorActionPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    $composeOutput = @(& docker compose -f $composeFile up -d 2>&1)
    $composeExitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $savedErrorActionPreference
}
if ($composeExitCode -ne 0) {
    $composeDetail = ($composeOutput | ForEach-Object { [string]$_ }) -join "; "
    throw "Qdrant docker compose startup failed with exit code $composeExitCode`: $composeDetail"
}
$lastError = "Qdrant HTTP readiness has not completed"
do {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $qdrantHealthUrl -TimeoutSec 5
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
