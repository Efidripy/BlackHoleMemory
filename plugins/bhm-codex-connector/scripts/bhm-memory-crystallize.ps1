param(
    [Parameter(Mandatory = $true)]
    [string]$ActionIds,
    [string]$Project = "e-github-workspace",
    [string]$SessionId = "",
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")
$sourceIds = @($ActionIds -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$crystalKey = if ([string]::IsNullOrWhiteSpace($SessionId)) {
    "crystal:${Project}:$($sourceIds -join '-')"
} else {
    "crystal:${Project}:$SessionId"
}
$result = Invoke-ConnectorJson -Method "POST" -Path "/bhm/crystallize" -Body @{
    source_ids = $sourceIds
    project = $Project
    title = "$Project crystal"
    summary = "crystallized from $($sourceIds.Count) source memories"
    upsert_key = $crystalKey
}
if ($AsJson) { $result | ConvertTo-Json -Depth 20; exit 0 }
$result
