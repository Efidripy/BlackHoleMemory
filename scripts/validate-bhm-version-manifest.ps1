[CmdletBinding()]
param(
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $repoRoot "config\version-manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "Version manifest is missing: $manifestPath"
}

$manifest = Get-Content -Raw -LiteralPath $manifestPath -Encoding UTF8 | ConvertFrom-Json
$release = [string]$manifest.release_version
$components = $manifest.components
$checks = @()

function Add-Check {
    param(
        [string]$Id,
        [bool]$Ok,
        [string]$Path,
        [string]$Expected
    )

    $script:checks += [ordered]@{
        id = $Id
        ok = $Ok
        path = $Path
        expected = $Expected
    }
}

function Read-Text {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    return Get-Content -Raw -LiteralPath $Path -Encoding UTF8
}

$packagePath = Join-Path $repoRoot "pyproject.toml"
$packageText = Read-Text $packagePath
Add-Check -Id "package" -Ok ($null -ne $packageText -and $packageText -match ('(?m)^version\s*=\s*"' + [regex]::Escape([string]$components.package) + '"\s*$')) -Path $packagePath -Expected ([string]$components.package)

$pluginPath = Join-Path $repoRoot "plugins\bhm-codex-connector\.codex-plugin\plugin.json"
$plugin = Get-Content -Raw -LiteralPath $pluginPath -Encoding UTF8 | ConvertFrom-Json
Add-Check -Id "plugin-source" -Ok ([string]$plugin.version -eq [string]$components.plugin) -Path $pluginPath -Expected ([string]$components.plugin)

$appPath = Join-Path $repoRoot "src\blackholememory\app.py"
$appText = Read-Text $appPath
$runtimeVersionSignal = $null -ne $appText -and $appText.Contains("RUNTIME_VERSION") -and ($appText.Contains('"version": RUNTIME_VERSION') -or $appText.Contains('version=RUNTIME_VERSION'))
Add-Check -Id "runtime-health" -Ok $runtimeVersionSignal -Path $appPath -Expected "RUNTIME_VERSION from version_manifest"
Add-Check -Id "broker" -Ok ($null -ne $appText -and $appText.Contains("BROKER_VERSION") -and $appText.Contains('"version": BROKER_VERSION')) -Path $appPath -Expected "BROKER_VERSION from version_manifest"

$openapiPath = Join-Path $repoRoot "src\blackholememory\openapi_contract.py"
$openapiText = Read-Text $openapiPath
Add-Check -Id "openapi" -Ok ($null -ne $openapiText -and $openapiText.Contains("RUNTIME_VERSION")) -Path $openapiPath -Expected "RUNTIME_VERSION from version_manifest"

foreach ($relativePath in @("src\blackholememory\static\index.html", "src\blackholememory\static\links.html")) {
    $path = Join-Path $repoRoot $relativePath
    $text = Read-Text $path
    Add-Check -Id ("ui-" + $relativePath.Replace('\', '/')) -Ok ($null -ne $text -and $text.Contains([string]$components.ui)) -Path $path -Expected ([string]$components.ui)
}

$launcherPath = Join-Path $repoRoot "scripts\bhm_launcher.py"
$launcherText = Read-Text $launcherPath
Add-Check -Id "launcher" -Ok ($null -ne $launcherText -and $launcherText.Contains("version-manifest.json") -and $launcherText.Contains("UI_VERSION")) -Path $launcherPath -Expected "UI_VERSION from version manifest"

$releaseScriptPath = Join-Path $repoRoot "scripts\build-release.ps1"
$releaseScriptText = Read-Text $releaseScriptPath
Add-Check -Id "release-default" -Ok ($null -ne $releaseScriptText -and $releaseScriptText.Contains(('Version = "v' + [string]$release))) -Path $releaseScriptPath -Expected ("v" + [string]$release)

$sourceConfigPath = Join-Path $repoRoot "config\plugin-source.json"
$sourceConfig = Get-Content -Raw -LiteralPath $sourceConfigPath -Encoding UTF8 | ConvertFrom-Json
Add-Check -Id "source-routing" -Ok ([string]$sourceConfig.version_manifest -eq "config/version-manifest.json") -Path $sourceConfigPath -Expected "config/version-manifest.json"

$workspaceRoot = Split-Path -Parent (Split-Path -Parent $repoRoot)
$userProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
if ([string]::IsNullOrWhiteSpace($userProfile)) {
    $userProfile = (Get-Location).Path
}
$generatedPluginPaths = @(
    (Join-Path $workspaceRoot "workspace\tools\codex-plugins\plugins\bhm-codex-connector\.codex-plugin\plugin.json"),
    (Join-Path $userProfile ".codex\plugins\local\bhm-codex-connector\.codex-plugin\plugin.json"),
    (Join-Path $userProfile (".codex\plugins\cache\bhm-marketplace\bhm-codex-connector\{0}\.codex-plugin\plugin.json" -f [string]$components.plugin))
)
$generatedIndex = 0
foreach ($path in $generatedPluginPaths) {
    $generatedIndex++
    $ok = $false
    if (Test-Path -LiteralPath $path) {
        $generated = Get-Content -Raw -LiteralPath $path -Encoding UTF8 | ConvertFrom-Json
        $ok = [string]$generated.version -eq [string]$components.plugin
    }
    else {
        # Generated plugin copies are machine-local outputs, not public source.
        # Public CI remains hermetic while validating any copies that are present.
        $ok = $true
    }
    Add-Check -Id ("generated-plugin-{0}" -f $generatedIndex) -Ok $ok -Path $path -Expected ([string]$components.plugin)
}

$failures = @($checks | Where-Object { -not $_.ok })
$result = [ordered]@{
    ok = $failures.Count -eq 0
    manifest = $manifest
    checks = $checks
    failures = $failures
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 20
}
else {
    Write-Host "=== BHM Version Manifest Gate ==="
    Write-Host ("Release: {0}" -f $release)
    foreach ($check in $checks) {
        Write-Host ("[{0}] {1}" -f ($(if ($check.ok) { "PASS" } else { "FAIL" }), $check.id))
    }
    Write-Host ("Summary: PASS={0} FAIL={1}" -f (@($checks | Where-Object ok).Count), $failures.Count)
}

if (-not $result.ok) {
    exit 1
}
