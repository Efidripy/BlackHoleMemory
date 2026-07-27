param(
    [ValidateSet("status", "install", "update", "rollback", "doctor", "native-attach")][string]$Action = "doctor",
    [string]$ReleaseArchive = "",
    [string]$TargetRoot = "",
    [string]$BackupRoot = "",
    [string]$BaseUrl = '',
    [string]$PythonPath = "",
    [switch]$Confirm,
    [switch]$DryRun,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot 'bhm-memory-common.ps1')
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Resolve-ConnectorBaseUrl }
$pluginRoot = Split-Path -Parent $PSScriptRoot
$candidates = @()
if ($env:BHM_INSTALL_ROOT) { $candidates += (Join-Path $env:BHM_INSTALL_ROOT "scripts\bhm-release-operator.ps1") }
if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA "BlackHoleMemory\resources\scripts\bhm-release-operator.ps1") }
$candidates += (Join-Path $pluginRoot "..\..\scripts\bhm-release-operator.ps1")
$operator = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $operator) {
    throw "BHM release operator was not found. Set BHM_INSTALL_ROOT or install the portable bundle."
}

$arguments = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Resolve-Path -LiteralPath $operator).Path,
    "-Action", $Action, "-BaseUrl", $BaseUrl, "-AsJson"
)
foreach ($name in @(@("ReleaseArchive", $ReleaseArchive), @("TargetRoot", $TargetRoot), @("BackupRoot", $BackupRoot), @("PythonPath", $PythonPath))) {
    if (-not [string]::IsNullOrWhiteSpace($name[1])) { $arguments += @("-" + $name[0], $name[1]) }
}
if ($Confirm) { $arguments += "-Confirm" }
if ($DryRun) { $arguments += "-DryRun" }
& powershell @arguments
exit $LASTEXITCODE
