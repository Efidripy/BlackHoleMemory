param(
    [ValidateRange(1, 30)][int]$TimeoutSec = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
$composeFile = Join-Path $repoRoot "infra\qdrant\docker-compose.yml"
$qdrantHealthUrl = Get-BhmRuntimeEndpoint -Name 'qdrant_http' -RepoRoot $repoRoot -Path 'healthz'

docker compose -f $composeFile up -d
Invoke-WebRequest -UseBasicParsing -TimeoutSec $TimeoutSec $qdrantHealthUrl | Select-Object -ExpandProperty Content
