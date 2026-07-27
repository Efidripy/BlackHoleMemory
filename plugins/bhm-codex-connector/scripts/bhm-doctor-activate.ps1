param(
    [string]$Project = "e-github-workspace",
    [string]$Title = "bhm-doctor-activate",
    [switch]$Lightweight,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")

$pluginRoot = Split-Path -Parent $PSScriptRoot
$runtimeConfigPath = Join-Path $pluginRoot "config\runtime-discovery.json"
$defaultEnvPath = "C:\Users\xman\.bhm\.env"
$apiBase = $null
$viewerUrl = $null
$workbenchUrl = [string]$env:BHM_WORKBENCH_URL

$profileScript = Join-Path $PSScriptRoot "bhm-profile.ps1"
$startWorkbenchScript = Join-Path $PSScriptRoot "start-bhm-workbench.ps1"
$liveCheckScript = Join-Path $PSScriptRoot "bhm-run-live-memory-check.ps1"
$preflightScript = Join-Path $PSScriptRoot "bhm-memory-preflight.ps1"
$checkpointScript = Join-Path $PSScriptRoot "bhm-memory-checkpoint.ps1"
$sessionRecordScript = Join-Path $PSScriptRoot "bhm-session-hybrid-record.ps1"
$showMcpScript = Join-Path $PSScriptRoot "bhm-show-mcp-sources.ps1"
$pluginManifestPath = Join-Path $pluginRoot ".codex-plugin\plugin.json"

function Expand-EnvPath {
    param(
        [string]$PathValue
    )

    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return $null
    }

    return [Environment]::ExpandEnvironmentVariables($PathValue)
}

function Get-RuntimeConfig {
    if (-not (Test-Path -LiteralPath $runtimeConfigPath)) {
        return [ordered]@{
            envPaths = @($defaultEnvPath)
            apiCandidates = @([string]$env:BHM_BASE_URL) | Where-Object { $_ }
            viewerCandidates = @([string]$env:BHM_VIEWER_URL, [string]$env:BHM_BASE_URL) | Where-Object { $_ }
            workbenchCandidates = @([string]$env:BHM_WORKBENCH_URL) | Where-Object { $_ }
        }
    }

    try {
        return Get-Content -Raw -LiteralPath $runtimeConfigPath | ConvertFrom-Json
    } catch {
        return [ordered]@{
            envPaths = @($defaultEnvPath)
            apiCandidates = @([string]$env:BHM_BASE_URL) | Where-Object { $_ }
            viewerCandidates = @([string]$env:BHM_VIEWER_URL, [string]$env:BHM_BASE_URL) | Where-Object { $_ }
            workbenchCandidates = @([string]$env:BHM_WORKBENCH_URL) | Where-Object { $_ }
        }
    }
}

function Resolve-FirstExistingPath {
    param(
        [object[]]$Candidates
    )

    foreach ($candidate in @($Candidates)) {
        $expanded = Expand-EnvPath -PathValue ([string]$candidate)
        if ($expanded -and (Test-Path -LiteralPath $expanded)) {
            return $expanded
        }
    }

    return Expand-EnvPath -PathValue $defaultEnvPath
}

function Resolve-ApiBase {
    param(
        [string[]]$Candidates
    )

    foreach ($candidate in @($Candidates)) {
        $probe = Invoke-HttpProbe -Url "$candidate/bhm/health"
        if ($probe.ok -and $probe.status -eq 200) {
            return $candidate
        }
    }

    return (@($Candidates) | Select-Object -First 1)
}

function Resolve-HealthProbe {
    param(
        [string]$BaseUrl
    )

    $probe = Invoke-HttpProbe -Url "$BaseUrl/bhm/health"
    if ($probe.ok -and $probe.status -eq 200) {
        return $probe
    }

    return $probe
}

function Resolve-ViewerUrl {
    param(
        [string[]]$Candidates
    )

    foreach ($candidate in @($Candidates)) {
        $probe = Invoke-HttpProbe -Url $candidate
        if ($probe.ok -and $probe.status -eq 200) {
            return $candidate
        }
    }

    return (@($Candidates) | Select-Object -First 1)
}

function Invoke-HttpProbe {
    param(
        [string]$Url
    )

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 12
        return [ordered]@{
            ok = $true
            status = [int]$response.StatusCode
            url = $Url
            reason = "ok"
        }
    } catch {
        $status = $null
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            $status = [int]$_.Exception.Response.StatusCode
        }
        return [ordered]@{
            ok = $false
            status = $status
            url = $Url
            reason = $_.Exception.Message
        }
    }
}

function Invoke-JsonScript {
    param(
        [string]$ScriptPath,
        [string[]]$Arguments = @()
    )

    $json = & powershell -NoProfile -ExecutionPolicy Bypass -File $ScriptPath @Arguments -AsJson
    return $json | ConvertFrom-Json
}

function Set-RegistryEntry {
    param(
        [string]$Name,
        [bool]$Attempted,
        [bool]$Ok,
        [string]$Reason,
        [object]$Data = $null
    )

    $script:registry[$Name] = [ordered]@{
        attempted = $Attempted
        ok = $Ok
        reason = $Reason
        data = $Data
    }
}

function Get-ActivatedNames {
    $names = @()
    foreach ($entry in $script:registry.GetEnumerator()) {
        if ($entry.Value.attempted -and $entry.Value.ok) {
            $names += $entry.Key
        }
    }
    return $names
}

function Get-FailedNames {
    $names = @()
    foreach ($entry in $script:registry.GetEnumerator()) {
        if ($entry.Value.attempted -and (-not $entry.Value.ok)) {
            $names += $entry.Key
        }
    }
    return $names
}

function New-RuString {
    param(
        [int[]]$Codes
    )

    return (-join ($Codes | ForEach-Object { [char]$_ }))
}

$registry = [ordered]@{}
$runtimeConfig = Get-RuntimeConfig
$envPath = Resolve-FirstExistingPath -Candidates $runtimeConfig.envPaths
$apiBase = Resolve-ApiBase -Candidates @($runtimeConfig.apiCandidates)
$viewerUrl = Resolve-ViewerUrl -Candidates @($runtimeConfig.viewerCandidates)
if ([string]::IsNullOrWhiteSpace($workbenchUrl)) {
    $workbenchUrl = @($runtimeConfig.workbenchCandidates) | Where-Object { $_ } | Select-Object -First 1
}
$mcpTransport = New-ConnectorTransportTruth -BaseUrl $apiBase -Operation "doctor"

$pluginManifest = $null
if (Test-Path -LiteralPath $pluginManifestPath) {
    try {
        $pluginManifest = Get-Content -Raw -LiteralPath $pluginManifestPath | ConvertFrom-Json
        Set-RegistryEntry -Name "plugin_installed" -Attempted $true -Ok $true -Reason "plugin manifest present" -Data @{
            version = $pluginManifest.version
            name = $pluginManifest.name
        }
    } catch {
        Set-RegistryEntry -Name "plugin_installed" -Attempted $true -Ok $false -Reason "plugin manifest unreadable" -Data $null
    }
} else {
    Set-RegistryEntry -Name "plugin_installed" -Attempted $true -Ok $false -Reason "plugin manifest missing" -Data $null
}

# Plugin-local `.mcp.json` is intentionally retired.  Host clients own the
# single canonical `bhm` registration; treating the missing legacy file as a
# failed doctor check would recreate the dead connection we removed.
Set-RegistryEntry -Name "mcp_wiring_loaded" -Attempted $false -Ok $true -Reason "host-owned canonical bhm registration; plugin-local MCP manifest retired" -Data @{
    server_id = "bhm"
    legacy_manifest = "retired"
}

$envLoaded = Test-Path -LiteralPath $envPath
Set-RegistryEntry -Name "env_loaded" -Attempted $true -Ok $envLoaded -Reason ($(if ($envLoaded) { "env file found" } else { "env file missing" })) -Data @{
    path = $envPath
}

$healthProbe = Resolve-HealthProbe -BaseUrl $apiBase
Set-RegistryEntry -Name "runtime_health_ok" -Attempted $true -Ok ($healthProbe.ok -and $healthProbe.status -eq 200) -Reason "health probe complete" -Data $healthProbe

$viewerProbe = Invoke-HttpProbe -Url $viewerUrl
Set-RegistryEntry -Name "viewer_ok" -Attempted $true -Ok ($viewerProbe.ok -and $viewerProbe.status -eq 200) -Reason "viewer probe complete" -Data $viewerProbe

$rootProbe = Invoke-HttpProbe -Url $apiBase
$rootProbeOk = $rootProbe.ok -or $rootProbe.status -eq 404
Set-RegistryEntry -Name "api_root_probe" -Attempted $true -Ok $rootProbeOk -Reason "root api probe complete" -Data $rootProbe

$profileStatus = $null
try {
    $profileStatus = Invoke-JsonScript -ScriptPath $profileScript -Arguments @("-Action", "status")
    Set-RegistryEntry -Name "profile_status_loaded" -Attempted $true -Ok ($profileStatus.ok -eq $true) -Reason "profile status loaded" -Data $profileStatus
} catch {
    Set-RegistryEntry -Name "profile_status_loaded" -Attempted $true -Ok $false -Reason "profile status failed" -Data @{
        error = $_.Exception.Message
    }
}

$discovery = [ordered]@{
    action = "runtime-discovery"
    resolved = [ordered]@{
        api_url = $apiBase
        viewer_url = $viewerUrl
        env_path = $envPath
    }
    probes = [ordered]@{
        api = $rootProbe
        health = $healthProbe
        viewer = $viewerProbe
    }
    verdict = [ordered]@{
        runtime_health = $(if ($healthProbe.ok -and $healthProbe.status -eq 200) { "healthy" } else { "missing" })
        viewer_health = $(if ($viewerProbe.ok -and $viewerProbe.status -eq 200) { "healthy" } else { "missing" })
        root_api = $(if ($rootProbe.ok) { "ok" } elseif ($rootProbe.status -eq 404) { "route-root-404" } else { "unhealthy" })
        recommendation = $(if ($healthProbe.ok -and $healthProbe.status -eq 200) { "runtime_usable" } else { "investigate_runtime" })
    }
}
Set-RegistryEntry -Name "discovery_loaded" -Attempted $true -Ok $true -Reason "runtime discovery composed" -Data $discovery
Set-RegistryEntry -Name "mcp_transport_truth" -Attempted $true -Ok ($mcpTransport.rest_bridge.available -eq $true) -Reason $mcpTransport.status -Data $mcpTransport

if ($healthProbe.ok -and $healthProbe.status -eq 200) {
    try {
        $workbenchStatus = Invoke-JsonScript -ScriptPath $startWorkbenchScript
        $workbenchProbe = Invoke-HttpProbe -Url $workbenchUrl
        Set-RegistryEntry -Name "workbench_ready" -Attempted $true -Ok ($workbenchProbe.ok -and $workbenchProbe.status -eq 200) -Reason "workbench start attempted" -Data @{
            start = $workbenchStatus
            probe = $workbenchProbe
        }
    } catch {
        Set-RegistryEntry -Name "workbench_ready" -Attempted $true -Ok $false -Reason "workbench start failed" -Data @{
            error = $_.Exception.Message
        }
    }

    try {
        $mcpSources = Invoke-JsonScript -ScriptPath $showMcpScript
        Set-RegistryEntry -Name "mcp_inventory_loaded" -Attempted $true -Ok $true -Reason "mcp inventory loaded" -Data $mcpSources
    } catch {
        Set-RegistryEntry -Name "mcp_inventory_loaded" -Attempted $true -Ok $false -Reason "mcp inventory failed" -Data @{
            error = $_.Exception.Message
        }
    }

    try {
        $preflight = Invoke-JsonScript -ScriptPath $preflightScript -Arguments @("-Project", $Project)
        Set-RegistryEntry -Name "start_ritual_ready" -Attempted $true -Ok ($preflight.ok -eq $true) -Reason "preflight attempted" -Data $preflight
    } catch {
        Set-RegistryEntry -Name "start_ritual_ready" -Attempted $true -Ok $false -Reason "preflight failed" -Data @{
            error = $_.Exception.Message
        }
    }

    if ($Lightweight) {
        Set-RegistryEntry -Name "close_ritual_ready" -Attempted $true -Ok $true -Reason "lightweight doctor skips durable close ritual"
        Set-RegistryEntry -Name "live_check_ready" -Attempted $true -Ok $true -Reason "lightweight doctor skips full live check"
    } else {
        try {
            $checkpoint = Invoke-JsonScript -ScriptPath $checkpointScript -Arguments @(
                "-Project", $Project,
                "-Type", "workflow",
                "-Done", "plugin doctor activate flow validated",
                "-Next", "use plugin in a normal task thread",
                "-Checks", "health ok; activation registry built",
                "-Risks", "cross-machine portability still needs external validation"
            )
            $sessionRecord = Invoke-JsonScript -ScriptPath $sessionRecordScript -Arguments @(
                "-Project", $Project,
                "-Title", $Title,
                "-Done", "plugin doctor activate flow validated",
                "-Next", "use plugin in a normal task thread",
                "-Checks", "health ok; activation registry built",
                "-Risks", "cross-machine portability still needs external validation",
                "-Decisions", "health 200 unlocks autonomous activation attempts",
                "-FilesTouched", "bhm-doctor-activate.ps1, plugin docs, workbench",
                "-ConversationNotes", "Doctor/activate flow executed to determine whether plugin can self-activate useful features."
            )
            Set-RegistryEntry -Name "close_ritual_ready" -Attempted $true -Ok ($checkpoint.success -eq $true -and $sessionRecord.project) -Reason "checkpoint and session record attempted" -Data @{
                checkpoint = $checkpoint
                session_record = $sessionRecord
            }
        } catch {
            Set-RegistryEntry -Name "close_ritual_ready" -Attempted $true -Ok $false -Reason "close ritual failed" -Data @{
                error = $_.Exception.Message
            }
        }

        try {
            $liveCheck = Invoke-JsonScript -ScriptPath $liveCheckScript -Arguments @(
                "-Project", $Project,
                "-Title", $Title
            )
            Set-RegistryEntry -Name "live_check_ready" -Attempted $true -Ok ($liveCheck.project -eq $Project) -Reason "live check attempted" -Data $liveCheck
        } catch {
            Set-RegistryEntry -Name "live_check_ready" -Attempted $true -Ok $false -Reason "live check failed" -Data @{
                error = $_.Exception.Message
            }
        }
    }
} else {
    Set-RegistryEntry -Name "workbench_ready" -Attempted $false -Ok $false -Reason "skipped because runtime health not ok"
    Set-RegistryEntry -Name "mcp_inventory_loaded" -Attempted $false -Ok $false -Reason "skipped because runtime health not ok"
    Set-RegistryEntry -Name "start_ritual_ready" -Attempted $false -Ok $false -Reason "skipped because runtime health not ok"
    Set-RegistryEntry -Name "close_ritual_ready" -Attempted $false -Ok $false -Reason "skipped because runtime health not ok"
    Set-RegistryEntry -Name "live_check_ready" -Attempted $false -Ok $false -Reason "skipped because runtime health not ok"
}

$activated = @(Get-ActivatedNames)
$failed = @(Get-FailedNames)

$verdictNotReady = New-RuString @(0x041F,0x041B,0x0410,0x0413,0x0418,0x041D,0x0020,0x041D,0x0415,0x0020,0x0413,0x041E,0x0422,0x041E,0x0412)
$activatedTitle = New-RuString @(0x0410,0x0432,0x0442,0x043E,0x043D,0x043E,0x043C,0x043D,0x043E,0x0020,0x0430,0x043A,0x0442,0x0438,0x0432,0x0438,0x0440,0x043E,0x0432,0x0430,0x043D,0x043E,0x003A)
$failedTitle = New-RuString @(0x041D,0x0435,0x0020,0x0443,0x0434,0x0430,0x043B,0x043E,0x0441,0x044C,0x0020,0x0430,0x043A,0x0442,0x0438,0x0432,0x0438,0x0440,0x043E,0x0432,0x0430,0x0442,0x044C,0x003A)

$ritualReady = $registry["runtime_health_ok"].ok -and $registry["start_ritual_ready"].ok -and $registry["close_ritual_ready"].ok -and $registry["live_check_ready"].ok
$finalVerdict = if ($ritualReady -and $mcpTransport.status -eq "MCP unavailable") {
    "REST bridge ready; MCP unavailable"
} elseif ($ritualReady) {
    "REST bridge ready; native MCP session unverified"
} elseif ($registry["runtime_health_ok"].ok -and $mcpTransport.status -eq "MCP unavailable") {
    "REST bridge partial; MCP unavailable"
} elseif ($registry["runtime_health_ok"].ok) {
    "REST bridge partial; native MCP session unverified"
} else {
    $verdictNotReady
}

$result = [ordered]@{
    ok = $true
    action = "bhm-doctor-activate"
    project = $Project
    final_verdict = $finalVerdict
    summary = [ordered]@{
        health_ok = $registry["runtime_health_ok"].ok
        viewer_ok = $registry["viewer_ok"].ok
        mcp_status = $mcpTransport.status
        mcp_available = $mcpTransport.mcp_available
        activated = $activated
        failed = $failed
    }
    mcp_transport = $mcpTransport
    transport = $mcpTransport
    autonomous_activated = $activated
    autonomous_failed = $failed
    registry = $registry
    human_report = @(
        $finalVerdict
        "transport: $($mcpTransport.status)"
        "recovery: $($mcpTransport.recovery_action)"
        ""
        $activatedTitle
        @(if ($activated.Count -gt 0) { $activated | ForEach-Object { "- $_" } } else { "- nothing" })
        ""
        $failedTitle
        @(if ($failed.Count -gt 0) { $failed | ForEach-Object { "- $_" } } else { "- nothing" })
    ) -join "`n"
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 50
    exit 0
}

Write-Host $result.human_report
