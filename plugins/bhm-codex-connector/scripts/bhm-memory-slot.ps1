param(
    [ValidateSet("list", "get", "create", "append", "replace", "delete", "reflect")]
    [string]$Action = "list",
    [string]$Project = "e-github-workspace",
    [string]$Label = "",
    [string]$Content = "",
    [string]$Text = "",
    [int]$SizeLimit = 2000,
    [string]$Description = "",
    [bool]$Pinned = $true,
    [ValidateSet("project", "global")]
    [string]$Scope = "project",
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")

$result = switch ($Action) {
    "list" { Invoke-ConnectorJson -Method "GET" -Path "/bhm/slots"; break }
    "get" {
        if ([string]::IsNullOrWhiteSpace($Label)) { throw "-Label is required for Action=get." }
        Invoke-ConnectorJson -Method "GET" -Path "/bhm/slot" -Query @{ label = $Label }
        break
    }
    "create" {
        if ([string]::IsNullOrWhiteSpace($Label)) { throw "-Label is required for Action=create." }
        Invoke-ConnectorJson -Method "POST" -Path "/bhm/slot" -Body @{
            label = $Label
            content = $Content
            sizeLimit = $SizeLimit
            description = $Description
            pinned = [bool]$Pinned
            scope = $Scope
        }
        break
    }
    "append" {
        if ([string]::IsNullOrWhiteSpace($Label) -or [string]::IsNullOrWhiteSpace($Text)) { throw "-Label and -Text are required for Action=append." }
        Invoke-ConnectorJson -Method "POST" -Path "/bhm/slot/append" -Body @{ label = $Label; text = $Text }
        break
    }
    "replace" {
        if ([string]::IsNullOrWhiteSpace($Label)) { throw "-Label is required for Action=replace." }
        Invoke-ConnectorJson -Method "POST" -Path "/bhm/slot/replace" -Body @{ label = $Label; content = $Content }
        break
    }
    "delete" {
        if ([string]::IsNullOrWhiteSpace($Label)) { throw "-Label is required for Action=delete." }
        Invoke-ConnectorJson -Method "DELETE" -Path "/bhm/slot" -Query @{ label = $Label }
        break
    }
    "reflect" {
        if ([string]::IsNullOrWhiteSpace($Label)) { throw "-Label is required for Action=reflect." }
        Invoke-ConnectorJson -Method "POST" -Path "/bhm/slot/reflect" -Body @{ label = $Label }
        break
    }
}

if ($AsJson) { $result | ConvertTo-Json -Depth 20; exit 0 }
$result
