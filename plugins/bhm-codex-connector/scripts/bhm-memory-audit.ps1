param(
    [string]$Operation = "",
    [int]$Limit = 50,
    [string]$Project = "e-github-workspace",
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")
$result = Invoke-ConnectorJson -Method "GET" -Path "/bhm/audit" -Query @{
    operation = $Operation
    limit = $Limit
}
if ($AsJson) { $result | ConvertTo-Json -Depth 20; exit 0 }
$result
