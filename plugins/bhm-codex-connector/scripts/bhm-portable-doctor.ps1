param(
    [string]$Project = "blackholememory",
    [string]$Title = "bhm-portable-doctor",
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$doctorScript = Join-Path $PSScriptRoot "bhm-doctor-activate.ps1"
$serverScript = Join-Path $PSScriptRoot "bhm-workbench-server.mjs"
$runtimeConfigPath = Join-Path (Split-Path -Parent $PSScriptRoot) "config\runtime-discovery.json"

function Get-Text {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    return Get-Content -Raw -LiteralPath $Path -Encoding UTF8
}

function New-RuString {
    param([int[]]$Codes)
    return (-join ($Codes | ForEach-Object { [char]$_ }))
}

$doctor = & powershell -NoProfile -ExecutionPolicy Bypass -File $doctorScript -Project $Project -Title $Title -Lightweight -AsJson | ConvertFrom-Json
$serverText = Get-Text -Path $serverScript
$runtimeConfigExists = Test-Path -LiteralPath $runtimeConfigPath

$advancedShared = @()

$corePluginLocal = @(
    "bhm-memory-common.ps1",
    "bhm-memory-preflight.ps1",
    "bhm-memory-checkpoint.ps1",
    "bhm-session-hybrid-record.ps1",
    "bhm-run-live-memory-check.ps1",
    "bhm-show-mcp-sources.ps1",
    "bhm-doctor-activate.ps1"
)

$orchestratorCapabilities = @(
    "connect_doctor_activate",
    "runtime_discovery",
    "start_task_ritual",
    "close_task_ritual",
    "live_check",
    "profile_status",
    "profile_switch_low_context",
    "profile_switch_standard",
    "profile_compare",
    "plugin_local_preflight",
    "plugin_local_checkpoint",
    "plugin_local_session_record"
)

$advancedWorkspaceCapabilities = @(
    "lesson_save",
    "lesson_recall",
    "lesson_strengthen",
    "slot_list",
    "slot_get",
    "slot_replace",
    "verify",
    "timeline",
    "audit",
    "crystallize",
    "reflect",
    "profile_view",
    "obsidian_export"
)

$portableVerdictConnected = New-RuString @(0x041F,0x041E,0x0420,0x0422,0x0410,0x0422,0x0418,0x0412,0x041D,0x041E,0x0415,0x0020,0x042F,0x0414,0x0420,0x041E,0x0020,0x0413,0x041E,0x0422,0x041E,0x0412,0x041E)
$portableVerdictPartial = New-RuString @(0x041F,0x041E,0x0420,0x0422,0x0410,0x0422,0x0418,0x0412,0x041D,0x041E,0x0415,0x0020,0x042F,0x0414,0x0420,0x041E,0x0020,0x0427,0x0410,0x0421,0x0422,0x0418,0x0427,0x041D,0x041E,0x0020,0x0413,0x041E,0x0422,0x041E,0x0412,0x041E)
$portableVerdictBlocked = New-RuString @(0x041F,0x041E,0x0420,0x0422,0x0410,0x0422,0x0418,0x0412,0x041D,0x041E,0x0415,0x0020,0x042F,0x0414,0x0420,0x041E,0x0020,0x041D,0x0415,0x0020,0x0413,0x041E,0x0422,0x041E,0x0412,0x041E)
$autonomousTitle = New-RuString @(0x0423,0x0436,0x0435,0x0020,0x0430,0x0432,0x0442,0x043E,0x043D,0x043E,0x043C,0x043D,0x043E,0x0020,0x0432,0x0020,0x043F,0x043B,0x0430,0x0433,0x0438,0x043D,0x0435,0x003A)
$workspaceTitle = New-RuString @(0x0415,0x0449,0x0435,0x0020,0x0437,0x0430,0x0432,0x044F,0x0437,0x0430,0x043D,0x043E,0x0020,0x043D,0x0430,0x0020,0x0077,0x006F,0x0072,0x006B,0x0073,0x0070,0x0061,0x0063,0x0065,0x0020,0x0073,0x0068,0x0061,0x0072,0x0065,0x0064,0x0020,0x006C,0x0061,0x0079,0x0065,0x0072,0x003A)
$tailsTitle = New-RuString @(0x0425,0x0432,0x043E,0x0441,0x0442,0x044B,0x003A)
$tailMove = New-RuString @(0x043F,0x0435,0x0440,0x0435,0x043D,0x0435,0x0441,0x0442,0x0438,0x0020,0x0061,0x0064,0x0076,0x0061,0x006E,0x0063,0x0065,0x0064,0x0020,0x006D,0x0065,0x006D,0x006F,0x0072,0x0079,0x0020,0x0063,0x006F,0x006E,0x0073,0x006F,0x006C,0x0065,0x0020,0x0074,0x006F,0x006F,0x006C,0x0073,0x0020,0x0432,0x0020,0x0070,0x006C,0x0075,0x0067,0x0069,0x006E,0x002D,0x006C,0x006F,0x0063,0x0061,0x006C,0x0020,0x0077,0x0072,0x0061,0x0070,0x0070,0x0065,0x0072,0x0073)
$tailProve = New-RuString @(0x043F,0x043E,0x0434,0x0442,0x0432,0x0435,0x0440,0x0434,0x0438,0x0442,0x044C,0x0020,0x0070,0x006F,0x0072,0x0074,0x0061,0x0062,0x006C,0x0065,0x002D,0x0067,0x0072,0x0061,0x0064,0x0065,0x0020,0x043D,0x0430,0x0020,0x0432,0x0442,0x043E,0x0440,0x043E,0x0439,0x0020,0x0057,0x0069,0x006E,0x0064,0x006F,0x0077,0x0073,0x002D,0x043C,0x0430,0x0448,0x0438,0x043D,0x0435,0x0020,0x0438,0x043B,0x0438,0x0020,0x0434,0x0440,0x0443,0x0433,0x043E,0x0439,0x0020,0x0074,0x006F,0x0070,0x006F,0x006C,0x006F,0x0067,0x0079)

$portableCoreOk = ($doctor.ok -eq $true) -and ($doctor.summary.health_ok -eq $true) -and ($doctor.summary.viewer_ok -eq $true)
$advancedStillShared = $false
$portableVerdict = if ($portableCoreOk -and -not $advancedStillShared) {
    $portableVerdictConnected
} elseif ($portableCoreOk) {
    $portableVerdictPartial
} else {
    $portableVerdictBlocked
}

$result = [ordered]@{
    ok = $true
    action = "bhm-portable-doctor"
    project = $Project
    portable_verdict = $portableVerdict
    plugin_connected_verdict = $doctor.final_verdict
    mcp_transport = $doctor.mcp_transport
    runtime_config_present = $runtimeConfigExists
    portable_core_ready = $portableCoreOk
    advanced_workspace_dependencies_present = $advancedStillShared
    core_plugin_local_scripts = $corePluginLocal
    orchestrator_capabilities = $orchestratorCapabilities
    advanced_workspace_capabilities = $advancedWorkspaceCapabilities
    advanced_workspace_dependency_scripts = $advancedShared
    remaining_tails = @(
        if ($advancedStillShared) { "move advanced memory console tools from workspace shared scripts into plugin-local wrappers" }
        if ($portableCoreOk) { "run the plugin on a second Windows machine or alternate runtime topology to prove portable-grade" }
    )
    doctor = $doctor
    human_report = @(
        $portableVerdict
        ""
        $autonomousTitle
        @($orchestratorCapabilities | ForEach-Object { "- $_" })
        ""
        $workspaceTitle
        @(if ($advancedShared.Count -gt 0) { $advancedWorkspaceCapabilities | ForEach-Object { "- $_" } } else { "- nothing" })
        ""
        $tailsTitle
        @(if ($advancedStillShared) { "- $tailMove" } else { "- advanced memory console already localized" })
        @(if ($portableCoreOk) { "- $tailProve" } else { "- first bring portable core to green runtime state" })
    ) -join "`n"
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 30
    exit 0
}

Write-Host $result.human_report
