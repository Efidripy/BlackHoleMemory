param(
    [string]$Project = "e-github-workspace",
    [string]$Title = "live-memory-check",
    [string]$Done = "live session check executed",
    [string]$Next = "review live-memory-check artifacts",
    [string]$Checks = "memory ritual end-to-end",
    [string]$Risks = "none recorded",
    [switch]$SkipSessionRecord,
    [switch]$AsJson
)

. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")

$baseUrl = Resolve-ConnectorBaseUrl

$artifactDir = Join-Path (Get-PluginDataRoot) "runtime\logs\live-memory-check"
New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null

function Save-JsonResult {
    param([string]$Path, [string]$JsonText)
    $parsed = $JsonText | ConvertFrom-Json
    ($parsed | ConvertTo-Json -Depth 40) | Set-Content -LiteralPath $Path -Encoding UTF8
    return $parsed
}

$preflightScript = Join-Path $PSScriptRoot "bhm-memory-preflight.ps1"
$sessionRecordScript = Join-Path $PSScriptRoot "bhm-session-hybrid-record.ps1"
$showMcpScript = Join-Path $PSScriptRoot "bhm-show-mcp-sources.ps1"

$result = [ordered]@{
    project = $Project
    title = $Title
    artifact_dir = $artifactDir
    preflight = $null
    mcp_sources = $null
    session_record = $null
    latest_session_log = $null
    transport = New-ConnectorTransportTruth -BaseUrl $baseUrl -Operation "live-memory-check"
    files = [ordered]@{}
}

$preflightJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $preflightScript -Project $Project -AsJson
$result.preflight = Save-JsonResult -Path (Join-Path $artifactDir "preflight.json") -JsonText $preflightJson
$result.files.preflight = Join-Path $artifactDir "preflight.json"

$mcpJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $showMcpScript -AsJson
$result.mcp_sources = Save-JsonResult -Path (Join-Path $artifactDir "mcp-sources.json") -JsonText $mcpJson
$result.files.mcp_sources = Join-Path $artifactDir "mcp-sources.json"

if (-not $SkipSessionRecord) {
    $sessionJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $sessionRecordScript `
        -Project $Project `
        -Title $Title `
        -Done $Done `
        -Next $Next `
        -Checks $Checks `
        -Risks $Risks `
        -AsJson
    $result.session_record = Save-JsonResult -Path (Join-Path $artifactDir "session-record.json") -JsonText $sessionJson
    $result.files.session_record = Join-Path $artifactDir "session-record.json"
    $result.latest_session_log = $result.session_record.session_log
}

$summaryLines = @(
    "Plugin Live Memory Check",
    "",
    "project: $Project",
    "title: $Title",
    "artifact_dir: $artifactDir",
    "latest_session_log: $($result.latest_session_log)",
    "",
    "files:",
    "- preflight.json",
    "- mcp-sources.json",
    "- session-record.json",
    "",
    "next: ask Codex to review the plugin live-memory-check bundle"
)
Set-Content -LiteralPath (Join-Path $artifactDir "README.txt") -Value $summaryLines -Encoding UTF8
$result.files.readme = Join-Path $artifactDir "README.txt"

if ($AsJson) {
    $result | ConvertTo-Json -Depth 20
    exit 0
}

Write-Host ($result | ConvertTo-Json -Depth 20)
