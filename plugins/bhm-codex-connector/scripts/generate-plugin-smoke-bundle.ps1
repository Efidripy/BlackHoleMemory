param(
    [string]$Project = "blackholememory",
    [string]$Title = "bhm-smoke",
    [string]$Done = "ran plugin-path smoke bundle",
    [string]$Next = "review plugin smoke bundle with Codex",
    [string]$Checks = "plugin install visible; live memory ritual invoked",
    [string]$Risks = "none recorded",
    [string]$WorkspaceRoot = "",
    [switch]$SkipSessionRecord,
    [switch]$Lightweight,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    $WorkspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
}

$sharedRunner = Join-Path $WorkspaceRoot "plugins\bhm-codex-connector\scripts\bhm-run-live-memory-check.ps1"
$artifactRoot = Join-Path ([System.IO.Path]::GetTempPath()) "bhm-plugin-smoke"
New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$bundlePath = Join-Path $artifactRoot ("bundle-{0}.json" -f $stamp)
$latestPath = Join-Path $artifactRoot "latest.json"

$runnerArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $sharedRunner,
    "-Project", $Project,
    "-Title", $Title,
    "-Done", $Done,
    "-Next", $Next,
    "-Checks", $Checks,
    "-Risks", $Risks,
    "-AsJson"
)

if ($SkipSessionRecord) {
    $runnerArgs += "-SkipSessionRecord"
}
if ($Lightweight) {
    $runnerArgs += "-SkipSessionRecord"
}

$runJson = & powershell @runnerArgs
$run = $runJson | ConvertFrom-Json

function Read-TextFileOrNull {
    param([string]$Path)
    if (-not [string]::IsNullOrWhiteSpace($Path) -and (Test-Path -LiteralPath $Path)) {
        return [System.IO.File]::ReadAllText($Path, [System.Text.UTF8Encoding]::new($false))
    }
    return $null
}

function Read-JsonFileOrNull {
    param([string]$Path)
    if (-not [string]::IsNullOrWhiteSpace($Path) -and (Test-Path -LiteralPath $Path)) {
        return Get-Content -Raw -LiteralPath $Path -Encoding UTF8 | ConvertFrom-Json
    }
    return $null
}

$bundle = [ordered]@{
    plugin = "bhm"
    generated_at = (Get-Date).ToString("o")
    project = $Project
    title = $Title
    artifact_dir = $run.artifact_dir
    source_files = $run.files
    preflight = Read-JsonFileOrNull -Path $run.files.preflight
    mcp_sources = Read-JsonFileOrNull -Path $run.files.mcp_sources
    session_record = Read-JsonFileOrNull -Path $run.files.session_record
    audit = Read-JsonFileOrNull -Path $run.files.audit
    guidance = Read-JsonFileOrNull -Path $run.files.guidance
    pending_items = Read-JsonFileOrNull -Path $run.files.pending_items
    latest_session_markdown = Read-TextFileOrNull -Path $run.files.latest_session
    worker_tail = Read-TextFileOrNull -Path $run.files.worker_tail
    lightweight = [bool]$Lightweight
}

$json = $bundle | ConvertTo-Json -Depth 40
[System.IO.File]::WriteAllText($bundlePath, $json, [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText($latestPath, $json, [System.Text.UTF8Encoding]::new($false))

$result = [ordered]@{
    ok = $true
    plugin = "bhm"
    bundle = $bundlePath
    latest = $latestPath
    artifact_dir = $artifactRoot
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 10
    exit 0
}

Write-Host "=== Plugin Smoke Bundle Ready ==="
Write-Host "- bundle      : $bundlePath"
Write-Host "- latest      : $latestPath"
Write-Host "- artifact dir: $artifactRoot"
Write-Host ""
Write-Host "Next: give Codex the file path or say 'проверь plugin smoke bundle'"
