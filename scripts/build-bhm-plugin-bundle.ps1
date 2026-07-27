[CmdletBinding()]
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

$repoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    $WorkspaceRoot = Split-Path -Parent (Split-Path -Parent $repoRoot)
}

$sourceRoot = Join-Path $repoRoot "plugins\bhm-codex-connector"
$sourceManifestPath = Join-Path $sourceRoot ".codex-plugin\plugin.json"
$sourceConfigPath = Join-Path $repoRoot "config\plugin-source.json"
$versionManifestPath = Join-Path $repoRoot "config\version-manifest.json"
$marketplaceTemplatePath = Join-Path $repoRoot "config\bhm-marketplace.json"

foreach ($requiredPath in @($sourceRoot, $sourceManifestPath, $sourceConfigPath, $versionManifestPath, $marketplaceTemplatePath)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required plugin source file is missing: $requiredPath"
    }
}

$sourceManifest = Get-Content -Raw -LiteralPath $sourceManifestPath -Encoding UTF8 | ConvertFrom-Json
$sourceConfig = Get-Content -Raw -LiteralPath $sourceConfigPath -Encoding UTF8 | ConvertFrom-Json
$versionManifest = Get-Content -Raw -LiteralPath $versionManifestPath -Encoding UTF8 | ConvertFrom-Json
$marketplaceJson = Get-Content -Raw -LiteralPath $marketplaceTemplatePath -Encoding UTF8
$pluginName = [string]$sourceManifest.name
$version = [string]$sourceManifest.version
if ($pluginName -ne [string]$sourceConfig.plugin_name) {
    throw "Plugin source manifest name does not match config: $pluginName"
}
if ([string]::IsNullOrWhiteSpace($version)) {
    throw "Plugin source manifest version is empty."
}
if ($version -ne [string]$versionManifest.components.plugin) {
    throw "Plugin source version '$version' does not match version manifest plugin version '$($versionManifest.components.plugin)'."
}

$timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
$backupRoot = Join-Path $WorkspaceRoot "workspace\runtime\logs\plugin-bundles\backups\$timestamp"

function Get-TargetDefinitions {
    param([string]$SelectedTarget)

    $definitions = @()
    if ($SelectedTarget -in @("workspace-marketplace", "all")) {
        $workspaceTarget = $sourceConfig.generated_targets | Where-Object { $_.id -eq "workspace-marketplace" }
        if ($null -eq $workspaceTarget) { throw "workspace-marketplace target is missing from plugin-source.json" }
        $definitions += [ordered]@{
            id = "workspace-marketplace"
            root = Join-Path $WorkspaceRoot ([string]$workspaceTarget.root)
            plugin_path = Join-Path (Join-Path $WorkspaceRoot ([string]$workspaceTarget.root)) ([string]$workspaceTarget.plugin_path)
            marketplace_path = Join-Path (Join-Path $WorkspaceRoot ([string]$workspaceTarget.root)) ([string]$workspaceTarget.marketplace_path)
        }
    }
    if ($SelectedTarget -in @("codex-local", "all")) {
        if ([string]::IsNullOrWhiteSpace($UserProfileRoot)) {
            throw "UserProfileRoot is required for codex-local target."
        }
        $localTarget = $sourceConfig.generated_targets | Where-Object { $_.id -eq "codex-local" }
        if ($null -eq $localTarget) { throw "codex-local target is missing from plugin-source.json" }
        $definitions += [ordered]@{
            id = "codex-local"
            root = Join-Path $UserProfileRoot ".codex\plugins\local"
            plugin_path = Join-Path $UserProfileRoot (Join-Path ".codex\plugins\local" ([string]$localTarget.plugin_path))
            marketplace_path = $null
        }
    }
    if ($SelectedTarget -in @("codex-cache", "all")) {
        if ([string]::IsNullOrWhiteSpace($UserProfileRoot)) {
            throw "UserProfileRoot is required for codex-cache target."
        }
        $cacheTarget = $sourceConfig.generated_targets | Where-Object { $_.id -eq "codex-cache" }
        if ($null -eq $cacheTarget) { throw "codex-cache target is missing from plugin-source.json" }
        $cacheRoot = Join-Path $UserProfileRoot ".codex\plugins\cache\bhm-marketplace\bhm-codex-connector\$version"
        $definitions += [ordered]@{
            id = "codex-cache"
            root = $cacheRoot
            plugin_path = $cacheRoot
            marketplace_path = $null
        }
    }
    return $definitions
}

function Backup-ExistingPath {
    param(
        [string]$Path,
        [string]$TargetId,
        [string]$Kind
    )

    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $targetBackupRoot = Join-Path $backupRoot $TargetId
    New-Item -ItemType Directory -Force -Path $targetBackupRoot | Out-Null
    $leaf = Split-Path -Leaf $Path
    $backupPath = Join-Path $targetBackupRoot "$Kind-$leaf"
    Copy-Item -LiteralPath $Path -Destination $backupPath -Recurse -Force
    return $backupPath
}

function Copy-PluginSource {
    param(
        [string]$Destination,
        [string]$TargetId
    )

    $parent = Split-Path -Parent $Destination
    $leaf = Split-Path -Leaf $Destination
    $stage = Join-Path $parent ".$leaf.stage-$PID"
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $sourceRoot -Force) {
        if ($sourceConfig.excluded_names -contains $item.Name) { continue }
        Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $stage $item.Name) -Recurse -Force
    }
    if (-not (Test-Path -LiteralPath (Join-Path $stage ".codex-plugin\plugin.json"))) {
        throw "Staged plugin is missing .codex-plugin/plugin.json"
    }
    $backup = Backup-ExistingPath -Path $Destination -TargetId $TargetId -Kind "plugin"
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    Move-Item -LiteralPath $stage -Destination $Destination
    return $backup
}

$targetDefinitions = @(Get-TargetDefinitions -SelectedTarget $Target)
if (-not $DryRun -and -not $Force) {
    $existingDestinations = @(
        foreach ($definition in $targetDefinitions) {
            if (Test-Path -LiteralPath ([string]$definition.plugin_path)) {
                [string]$definition.plugin_path
            }
            if ($definition.marketplace_path -and (Test-Path -LiteralPath ([string]$definition.marketplace_path))) {
                [string]$definition.marketplace_path
            }
        }
    )
    if ($existingDestinations.Count -gt 0) {
        throw "Generated target already exists; re-run with -Force after reviewing: $($existingDestinations -join '; ')"
    }
}

$records = @()
foreach ($definition in $targetDefinitions) {
    $pluginPath = [string]$definition.plugin_path
    $marketplacePath = [string]$definition.marketplace_path
    $record = [ordered]@{
        id = $definition.id
        plugin_path = $pluginPath
        marketplace_path = if ($marketplacePath) { $marketplacePath } else { $null }
        action = if ($DryRun) { "planned" } else { "generated" }
        plugin_backup = $null
        marketplace_backup = $null
    }

    if ($DryRun) {
        $records += $record
        continue
    }

    $pluginParent = Split-Path -Parent $pluginPath
    New-Item -ItemType Directory -Force -Path $pluginParent | Out-Null
    $record.plugin_backup = Copy-PluginSource -Destination $pluginPath -TargetId ([string]$definition.id)

    if ($marketplacePath) {
        $marketplaceParent = Split-Path -Parent $marketplacePath
        New-Item -ItemType Directory -Force -Path $marketplaceParent | Out-Null
        $record.marketplace_backup = Backup-ExistingPath -Path $marketplacePath -TargetId ([string]$definition.id) -Kind "marketplace"
        [System.IO.File]::WriteAllText(
            $marketplacePath,
            $marketplaceJson,
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    $records += $record
}

$result = [ordered]@{
    ok = $true
    plugin = $pluginName
    version = $version
    source = $sourceRoot
    target = $Target
    force = [bool]$Force
    dry_run = [bool]$DryRun
    bundles = $records
    backup_root = if (-not $DryRun) { $backupRoot } else { $null }
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 20
    exit 0
}

$result | ConvertTo-Json -Depth 20
