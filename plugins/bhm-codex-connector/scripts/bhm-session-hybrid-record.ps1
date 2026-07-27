param(
    [string]$Project = "e-github-workspace",
    [string]$Title = "session",
    [string]$Done,
    [string]$Next,
    [string]$Checks,
    [string]$Risks,
    [string]$Decisions,
    [string]$FilesTouched,
    [string]$ConversationNotes,
    [string]$TranscriptRef,
    [string]$UpsertKey = "",
    [switch]$SkipMemory,
    [switch]$AsJson
)

. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")

$baseUrl = Resolve-ConnectorBaseUrl

function Format-Value {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return "not recorded" }
    return $Value
}

$sessionDir = Get-ConnectorSessionDir -Project $Project
$timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$titleSlug = ConvertTo-ConnectorSlug -Value $Title
$sessionPath = Join-Path $sessionDir "session-$timestamp-$titleSlug.md"
$dataRoot = Get-PluginDataRoot
$relativePath = $sessionPath.Replace("$dataRoot\", "")

$content = @(
    "# Hybrid Session Record",
    "",
    "- project: $Project",
    "- title: $Title",
    "- created_at: $(Get-Date -Format s)",
    "- memory_project: $Project",
    "- transcript_ref: $(Format-Value -Value $TranscriptRef)",
    "",
    "## Done",
    "",
    "$(Format-Value -Value $Done)",
    "",
    "## Next",
    "",
    "$(Format-Value -Value $Next)",
    "",
    "## Checks",
    "",
    "$(Format-Value -Value $Checks)",
    "",
    "## Risks / Notes",
    "",
    "$(Format-Value -Value $Risks)",
    "",
    "## Decisions",
    "",
    "$(Format-Value -Value $Decisions)",
    "",
    "## Files Touched",
    "",
    "$(Format-Value -Value $FilesTouched)",
    "",
    "## Conversation Notes",
    "",
    "$(Format-Value -Value $ConversationNotes)"
)
Set-Content -LiteralPath $sessionPath -Value $content -Encoding UTF8

$memoryResult = $null
if (-not $SkipMemory) {
    $payload = @{
        project = $Project
        title = $Title
        done = (Format-Value -Value $Done)
        next = (Format-Value -Value $Next)
        checks = (Format-Value -Value $Checks)
        risks = (Format-Value -Value $Risks)
        decisions = (Format-Value -Value $Decisions)
        files_touched = @($relativePath)
        conversation_notes = (Format-Value -Value $ConversationNotes)
        transcript_ref = (Format-Value -Value $TranscriptRef)
        upsert_key = $UpsertKey
    }
    $memoryResult = Invoke-ConnectorJson -Method "POST" -Path "/bhm/session-record" -Body $payload -BaseUrl $baseUrl
}

$transport = New-ConnectorTransportTruth -BaseUrl $baseUrl -Operation "session-record"

$result = [pscustomobject]@{
    project = $Project
    session_log = $sessionPath
    relative_path = $relativePath
    memory = if ($memoryResult) { $memoryResult } else { $null }
    transport = $transport
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 20
    exit 0
}

Write-Host ($result | ConvertTo-Json -Depth 20)
