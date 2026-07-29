[CmdletBinding()]
param(
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pyprojectPath = Join-Path $repoRoot "pyproject.toml"
$lockPath = Join-Path $repoRoot "uv.lock"
$versionManifestPath = Join-Path $repoRoot "config\version-manifest.json"
$checks = @()

function Add-Check {
    param(
        [string]$Id,
        [bool]$Ok,
        [string]$Detail
    )
    $script:checks += [ordered]@{ id = $Id; ok = $Ok; detail = $Detail }
}

Add-Check -Id "pyproject" -Ok (Test-Path -LiteralPath $pyprojectPath) -Detail $pyprojectPath
Add-Check -Id "uv-lock-exists" -Ok (Test-Path -LiteralPath $lockPath) -Detail $lockPath

$pyprojectText = Get-Content -Raw -LiteralPath $pyprojectPath -Encoding UTF8
$lockText = Get-Content -Raw -LiteralPath $lockPath -Encoding UTF8
$manifest = Get-Content -Raw -LiteralPath $versionManifestPath -Encoding UTF8 | ConvertFrom-Json

$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$lockOutput = @(& uv lock --check 2>&1)
$lockExit = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
Add-Check -Id "uv-lock-check" -Ok ($lockExit -eq 0) -Detail (($lockOutput -join " ").Trim())

$testExtraOk = $pyprojectText -match '(?ms)\[project\.optional-dependencies\].*?test\s*=\s*\[.*?pytest==8\.4\.2.*?pytest-cov==7\.1\.0.*?\]'
$devExtraOk = $pyprojectText -match '(?ms)\[project\.optional-dependencies\].*?dev\s*=\s*\[.*?pytest==8\.4\.2.*?pytest-cov==7\.1\.0.*?ruff==0\.15\.10.*?\]'
$buildExtraOk = $pyprojectText -match '(?ms)\[project\.optional-dependencies\].*?build\s*=\s*\[.*?PyQt6==6\.11\.0.*?pyinstaller==6\.21\.0.*?\]'
Add-Check -Id "test-extra" -Ok $testExtraOk -Detail "pytest and pytest-cov are pinned in [project.optional-dependencies].test"
Add-Check -Id "dev-extra" -Ok $devExtraOk -Detail "pytest, pytest-cov and ruff are pinned in [project.optional-dependencies].dev"
Add-Check -Id "build-extra" -Ok $buildExtraOk -Detail "PyQt6 and pyinstaller are pinned in [project.optional-dependencies].build"

$lockProjectVersion = [regex]::Escape([string]$manifest.components.package)
$lockProjectOk = $lockText -match ('(?ms)name = "blackholememory".*?version = "' + $lockProjectVersion + '".*?\[package\.optional-dependencies\].*?build = .*?dev = .*?test = ')
$lockToolsOk = $lockText.Contains('name = "pytest"') -and $lockText.Contains('name = "pytest-cov"') -and $lockText.Contains('name = "ruff"')
$lockBuildOk = $lockText.Contains('name = "pyinstaller"') -and $lockText.Contains('name = "pyqt6"')
Add-Check -Id "lock-project-extras" -Ok $lockProjectOk -Detail ("uv.lock contains blackholememory {0} with dev/test extras" -f [string]$manifest.components.package)
Add-Check -Id "lock-tool-entries" -Ok $lockToolsOk -Detail "uv.lock contains pytest, pytest-cov and ruff entries"
Add-Check -Id "lock-build-entry" -Ok $lockBuildOk -Detail "uv.lock contains the pinned PyQt6 and pyinstaller build entries"

$failures = @($checks | Where-Object { -not $_.ok })
$result = [ordered]@{
    ok = $failures.Count -eq 0
    lock = $lockPath
    checks = $checks
    failures = $failures
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 20
}
else {
    Write-Host "=== BHM Dependency Lock Gate ==="
    foreach ($check in $checks) {
        Write-Host ("[{0}] {1}: {2}" -f ($(if ($check.ok) { "PASS" } else { "FAIL" }), $check.id, $check.detail))
    }
}

if (-not $result.ok) {
    exit 1
}
