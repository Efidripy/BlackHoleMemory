param(
    [ValidateSet("workspace-marketplace", "codex-local", "codex-cache", "all")]
    [string]$Target = "workspace-marketplace",
    [string]$WorkspaceRoot = "",
    [string]$UserProfileRoot = $env:USERPROFILE,
    [switch]$Force,
    [switch]$DryRun,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$builder = Join-Path $repoRoot "scripts\build-bhm-plugin-bundle.ps1"
if (-not (Test-Path -LiteralPath $builder)) {
    throw "Canonical plugin bundle builder is missing: $builder"
}

$builderArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $builder,
    "-Target", $Target
)
if (-not [string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    $builderArgs += @("-WorkspaceRoot", $WorkspaceRoot)
}
if (-not [string]::IsNullOrWhiteSpace($UserProfileRoot)) {
    $builderArgs += @("-UserProfileRoot", $UserProfileRoot)
}
if ($Force) { $builderArgs += "-Force" }
if ($DryRun) { $builderArgs += "-DryRun" }
if ($AsJson) { $builderArgs += "-AsJson" }

& powershell @builderArgs
exit $LASTEXITCODE
