param(
    [string]$Project = "e-github-workspace",
    [ValidateSet("fact", "workflow", "pattern", "bug", "architecture", "preference")]
    [string]$Type = "workflow",
    [string]$Content,
    [string]$Done,
    [string]$Next,
    [string]$Checks,
    [string]$Risks,
    [string]$Title = "",
    [string]$Concepts = "checkpoint,bhm,workspace",
    [string]$Files = "",
    [string]$UpsertKey = "",
    [switch]$AsJson
)

. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")

$baseUrl = Resolve-ConnectorBaseUrl

function Split-Csv {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return [string[]]@() }
    return [string[]]@($Value -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

if ([string]::IsNullOrWhiteSpace($Content)) {
    if ([string]::IsNullOrWhiteSpace($Done) -and [string]::IsNullOrWhiteSpace($Next) -and [string]::IsNullOrWhiteSpace($Checks) -and [string]::IsNullOrWhiteSpace($Risks)) {
        throw "Provide -Content or at least one of -Done, -Next, -Checks, -Risks."
    }
    $Content = @(
        "$Project checkpoint:",
        "done: $Done",
        "next: $Next",
        "checks: $Checks",
        "risks/notes: $Risks"
    ) -join "`n"
}

$body = @{
    project = $Project
    checkpoint_type = $Type
    title = $Title
    content = $Content
    done = $Done
    next = $Next
    checks = $Checks
    risks = $Risks
    concepts = [string[]](Split-Csv -Value $Concepts)
    files = [string[]](Split-Csv -Value $Files)
    upsert_key = $UpsertKey
}

$result = Invoke-ConnectorJson -Method "POST" -Path "/bhm/checkpoint" -Body $body -BaseUrl $baseUrl
$transport = New-ConnectorTransportTruth -BaseUrl $baseUrl -Operation "checkpoint"

$checkpoint = $result.checkpoint
$memory = [pscustomobject]@{
    id = $checkpoint.memory_id
    title = $checkpoint.title
    project = $checkpoint.project
    type = $checkpoint.checkpoint_type
    createdAt = $checkpoint.created_at
}

if ($AsJson) {
    [pscustomobject]@{
        success = $result.success
        action = $result.action
        checkpoint = $checkpoint
        memory = $memory
        transport = $transport
        lesson = $null
    } | ConvertTo-Json -Depth 20
    exit 0
}

Write-Host ([pscustomobject]@{ success = $result.success; action = $result.action; checkpoint = $checkpoint; memory = $memory; transport = $transport } | ConvertTo-Json -Depth 20)
