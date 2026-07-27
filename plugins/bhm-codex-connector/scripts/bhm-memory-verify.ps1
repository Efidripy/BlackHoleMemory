param(
    [Parameter(Mandatory = $true)]
    [string]$Id,
    [string]$Project = "e-github-workspace",
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")
$result = Invoke-ConnectorJson -Method "POST" -Path "/bhm/verify" -Body @{ id = $Id }
if ($AsJson) { $result | ConvertTo-Json -Depth 20; exit 0 }
$result
