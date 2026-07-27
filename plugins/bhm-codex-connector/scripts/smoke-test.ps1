param(
    [string]$BaseUrl = ''
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot 'bhm-memory-common.ps1')
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Resolve-ConnectorBaseUrl }

try {
    $health = Invoke-RestMethod -Uri "$BaseUrl/bhm/health" -Method Get
} catch {
    throw
}

[pscustomobject]@{
    ok = $true
    service = $health.service
    status = $health.status
    version = $health.version
    viewerPort = $health.viewerPort
} | ConvertTo-Json -Depth 10
