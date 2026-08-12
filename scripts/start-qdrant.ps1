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

docker compose -f $composeFile up -d
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
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
