param(
    [ValidateSet("reflect", "insights", "search")]
    [string]$Action = "insights",
    [string]$Project = "e-github-workspace",
    [int]$MaxClusters = 10,
    [double]$MinConfidence = 0,
    [int]$Limit = 20,
    [string]$Query = "",
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")

$result = switch ($Action) {
    "reflect" {
        Invoke-ConnectorJson -Method "POST" -Path "/bhm/reflect" -Body @{
            project = $Project
            maxClusters = $MaxClusters
        }
        break
    }
    "insights" {
        Invoke-ConnectorJson -Method "GET" -Path "/bhm/insights" -Query @{
            project = $Project
            minConfidence = $MinConfidence
            limit = $Limit
        }
        break
    }
    "search" {
        if ([string]::IsNullOrWhiteSpace($Query)) { throw "-Query is required for Action=search." }
        Invoke-ConnectorJson -Method "POST" -Path "/bhm/insights/search" -Body @{
            query = $Query
            project = $Project
            minConfidence = $MinConfidence
            limit = $Limit
        }
        break
    }
}

if ($AsJson) { $result | ConvertTo-Json -Depth 20; exit 0 }
$result
