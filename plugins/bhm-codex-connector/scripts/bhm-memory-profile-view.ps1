param(
    [string]$Project = "e-github-workspace",
    [switch]$Refresh,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")
$result = Invoke-ConnectorJson -Method "GET" -Path "/bhm/profile" -Query @{
    project = $Project
    refresh = if ($Refresh) { "true" } else { "" }
}
if ($AsJson) { $result | ConvertTo-Json -Depth 20; exit 0 }
$result
