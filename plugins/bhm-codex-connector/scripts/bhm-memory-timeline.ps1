param(
    [Parameter(Mandatory = $true)]
    [string]$Anchor,
    [string]$Project = "e-github-workspace",
    [int]$Before = 5,
    [int]$After = 5,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")
$result = Invoke-ConnectorJson -Method "POST" -Path "/bhm/memory/timeline" -Body @{
    concept = $Anchor
    project = $Project
    limit = [Math]::Max(1, $Before + $After + 1)
    include_archived = $false
}
if ($AsJson) { $result | ConvertTo-Json -Depth 20; exit 0 }
$result
