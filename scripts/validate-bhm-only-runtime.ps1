param(
    [string]$BaseUrl = '',
    [string]$WorkspaceRoot = "E:\GitHub",
    [switch]$AsJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\runtime-endpoints.ps1')
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot (Split-Path -Parent $PSScriptRoot) }

$checks = [System.Collections.Generic.List[object]]::new()

function Add-Check {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Detail,
        [object]$Evidence = $null
    )

    $checks.Add([pscustomobject]@{
        name = $Name
        ok = $Ok
        detail = $Detail
        evidence = $Evidence
    })
}

function Get-ForbiddenRouteMatches {
    param([string[]]$Roots)

    $extensions = @(
        ".ps1", ".cmd", ".bat", ".py", ".js", ".mjs",
        ".json", ".toml", ".yaml", ".yml"
    )
    $matches = @()

    foreach ($root in $Roots) {
        if (-not (Test-Path -LiteralPath $root)) {
            continue
        }

        $files = Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $extensions -contains $_.Extension.ToLowerInvariant() }

        foreach ($file in $files) {
            if ($file.FullName -eq $PSCommandPath) {
                continue
            }
            $hits = Select-String -LiteralPath $file.FullName -SimpleMatch "/agentmemory/" -ErrorAction SilentlyContinue
            foreach ($hit in @($hits)) {
                $matches += [pscustomobject]@{
                    path = $file.FullName
                    line = $hit.LineNumber
                }
            }
        }
    }

    return @($matches)
}

function Get-ActiveConfigRetiredInstructions {
    param([string[]]$Paths)

    $matches = @()
    $pattern = "(?i)\b(search|use|load|connect|query|recall)\b.{0,80}\bAgentMemory\b"

    foreach ($path in $Paths) {
        if (-not (Test-Path -LiteralPath $path)) {
            continue
        }

        $hits = Select-String -LiteralPath $path -Pattern $pattern -ErrorAction SilentlyContinue
        foreach ($hit in @($hits)) {
            $matches += [pscustomobject]@{
                path = $path
                line = $hit.LineNumber
            }
        }
    }

    return @($matches)
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoots = @(
    (Join-Path $WorkspaceRoot "workspace\control\scripts\shared"),
    (Join-Path $WorkspaceRoot "workspace\tools\codex-plugins\plugins\bhm-codex-connector"),
    (Join-Path $repoRoot "src"),
    (Join-Path $repoRoot "scripts"),
    (Join-Path $repoRoot "plugins")
)

$forbiddenRouteMatches = @(Get-ForbiddenRouteMatches -Roots $runtimeRoots)
Add-Check -Name "forbidden_route_absence" -Ok ($forbiddenRouteMatches.Count -eq 0) -Detail "Executable normal-runtime files must not reference retired routes." -Evidence $forbiddenRouteMatches

$activeConfigPaths = @(
    (Join-Path $WorkspaceRoot ".claude\settings.json"),
    (Join-Path $WorkspaceRoot ".codex\config.toml"),
    (Join-Path $env:USERPROFILE ".claude\settings.json"),
    (Join-Path $env:USERPROFILE ".codex\config.toml")
)
$retiredInstructions = @(Get-ActiveConfigRetiredInstructions -Paths $activeConfigPaths)
Add-Check -Name "active_config_bhm_first" -Ok ($retiredInstructions.Count -eq 0) -Detail "Active agent configuration must not instruct agents to use AgentMemory." -Evidence $retiredInstructions

$forbiddenNamedSharedScripts = @()
$sharedScripts = Join-Path $WorkspaceRoot "workspace\control\scripts\shared"
if (Test-Path -LiteralPath $sharedScripts) {
    $forbiddenNamedSharedScripts = @(
        Get-ChildItem -LiteralPath $sharedScripts -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match "(?i)agentmemory" } |
            Select-Object -ExpandProperty FullName
    )
}
Add-Check -Name "shared_script_names" -Ok ($forbiddenNamedSharedScripts.Count -eq 0) -Detail "Normal shared script entrypoints must use BHM naming." -Evidence $forbiddenNamedSharedScripts

$openapi = $null
try {
    $openapi = Invoke-RestMethod -Method Get -Uri "$BaseUrl/openapi.json" -TimeoutSec 20
    Add-Check -Name "openapi_reachable" -Ok $true -Detail "BHM OpenAPI is reachable."
} catch {
    Add-Check -Name "openapi_reachable" -Ok $false -Detail $_.Exception.Message
}

if ($null -ne $openapi) {
    $pathNames = @($openapi.paths.PSObject.Properties | ForEach-Object { $_.Name })
    $forbiddenPaths = @($pathNames | Where-Object { $_ -like "/agentmemory/*" })
    Add-Check -Name "openapi_has_no_retired_routes" -Ok ($forbiddenPaths.Count -eq 0) -Detail "Live OpenAPI must not register retired routes." -Evidence $forbiddenPaths

    $requiredPaths = @(
        "/bhm/health",
        "/health/ready",
        "/health/cutover",
        "/bhm/search",
        "/bhm/remember",
        "/bhm/observe",
        "/bhm/session-record"
    )
    $missingPaths = @($requiredPaths | Where-Object { $_ -notin $pathNames })
    Add-Check -Name "required_bhm_routes" -Ok ($missingPaths.Count -eq 0) -Detail "Required BHM ritual and health routes must be registered." -Evidence $missingPaths
}

try {
    $health = Invoke-RestMethod -Method Get -Uri "$BaseUrl/bhm/health" -TimeoutSec 10
    Add-Check -Name "bhm_health" -Ok ($health.status -eq "healthy") -Detail "BHM health must be healthy." -Evidence $health
} catch {
    Add-Check -Name "bhm_health" -Ok $false -Detail $_.Exception.Message
}

try {
    $ready = Invoke-RestMethod -Method Get -Uri "$BaseUrl/health/ready" -TimeoutSec 10
    Add-Check -Name "bhm_ready" -Ok ([bool]$ready.ok) -Detail "BHM readiness must be green." -Evidence $ready
} catch {
    Add-Check -Name "bhm_ready" -Ok $false -Detail $_.Exception.Message
}

try {
    $cutover = Invoke-RestMethod -Method Get -Uri "$BaseUrl/health/cutover" -TimeoutSec 10
    Add-Check -Name "bhm_cutover_health" -Ok ([bool]$cutover.ok) -Detail "BHM cutover health must be green." -Evidence $cutover
} catch {
    Add-Check -Name "bhm_cutover_health" -Ok $false -Detail $_.Exception.Message
}

$failed = @($checks | Where-Object { -not $_.ok })
$result = [pscustomobject]@{
    ok = ($failed.Count -eq 0)
    generated_at = (Get-Date).ToString("o")
    base_url = $BaseUrl
    checks_total = $checks.Count
    checks_failed = $failed.Count
    checks = $checks
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 20
} else {
    foreach ($check in $checks) {
        $status = if ($check.ok) { "PASS" } else { "FAIL" }
        Write-Output ("[{0}] {1}: {2}" -f $status, $check.name, $check.detail)
    }
    Write-Output ("Summary: PASS={0} FAIL={1}" -f ($checks.Count - $failed.Count), $failed.Count)
}

if (-not $result.ok) {
    exit 1
}
