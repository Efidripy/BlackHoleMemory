param(
    [ValidateSet("save", "recall", "list", "strengthen")]
    [string]$Action = "recall",
    [string]$Project = "e-github-workspace",
    [string]$Content,
    [string]$Context = "",
    [double]$Confidence = 0.5,
    [string]$Query = "",
    [string]$Tags = "",
    [string]$LessonId = "",
    [double]$MinConfidence = 0.1,
    [int]$Limit = 10,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")

$result = switch ($Action) {
    "save" {
        if ([string]::IsNullOrWhiteSpace($Content)) { throw "-Content is required for Action=save." }
        Invoke-ConnectorJson -Method "POST" -Path "/bhm/lessons" -Body @{
            content = $Content
            context = $Context
            confidence = $Confidence
            project = $Project
            tags = $Tags
        }
        break
    }
    "recall" {
        if ([string]::IsNullOrWhiteSpace($Query)) { throw "-Query is required for Action=recall." }
        Invoke-ConnectorJson -Method "POST" -Path "/bhm/lessons/search" -Body @{
            query = $Query
            project = $Project
            minConfidence = $MinConfidence
            limit = $Limit
        }
        break
    }
    "list" {
        Invoke-ConnectorJson -Method "GET" -Path "/bhm/lessons" -Query @{
            project = $Project
            minConfidence = $MinConfidence
            limit = $Limit
        }
        break
    }
    "strengthen" {
        if ([string]::IsNullOrWhiteSpace($LessonId)) { throw "-LessonId is required for Action=strengthen." }
        Invoke-ConnectorJson -Method "POST" -Path "/bhm/lessons/strengthen" -Body @{ lessonId = $LessonId }
        break
    }
}

if ($AsJson) { $result | ConvertTo-Json -Depth 20; exit 0 }
$result
