param(
    [string]$VaultDir = "$env:USERPROFILE\.codex\plugin-data\bhm\runtime\exports\bhm-obsidian",
    [string]$Types = "memories,lessons,crystals,sessions",
    [string]$Project = $(if ($env:BHM_PROJECT) { $env:BHM_PROJECT } else { Split-Path -Leaf (Get-Location) }),
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")
$result = Invoke-ConnectorJson -Method "POST" -Path "/bhm/obsidian/export" -Body @{
    vaultDir = $VaultDir
    types = $Types
}
if ($AsJson) { $result | ConvertTo-Json -Depth 20; exit 0 }
$result
