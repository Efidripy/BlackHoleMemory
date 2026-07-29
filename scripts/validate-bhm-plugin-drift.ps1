[CmdletBinding()]
param(
    [string]$WorkspaceRoot = "",
    [string]$UserProfileRoot = $env:USERPROFILE,
    [switch]$RequireCache,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    $WorkspaceRoot = Split-Path -Parent (Split-Path -Parent $repoRoot)
}

$sourceConfigPath = Join-Path $repoRoot "config\plugin-source.json"
$sourceManifestPath = Join-Path $repoRoot "plugins\bhm-codex-connector\.codex-plugin\plugin.json"
if (-not (Test-Path -LiteralPath $sourceConfigPath)) { throw "Plugin source config is missing: $sourceConfigPath" }
if (-not (Test-Path -LiteralPath $sourceManifestPath)) { throw "Plugin source manifest is missing: $sourceManifestPath" }

$sourceConfig = Get-Content -Raw -LiteralPath $sourceConfigPath -Encoding UTF8 | ConvertFrom-Json
$sourceManifest = Get-Content -Raw -LiteralPath $sourceManifestPath -Encoding UTF8 | ConvertFrom-Json
$sourceRoot = Join-Path $repoRoot ([string]$sourceConfig.source_of_truth)
$marketplaceTemplate = Join-Path $repoRoot ([string]$sourceConfig.marketplace_template)
$excludedNames = @($sourceConfig.excluded_names | ForEach-Object { [string]$_ })

function Get-Inventory {
    param([string]$Root)

    $inventory = @{}
    if (-not (Test-Path -LiteralPath $Root)) { return $inventory }
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -Force) {
        $relative = $file.FullName.Substring($Root.Length).TrimStart('\', '/')
        $parts = $relative -split '[\\/]'
        if (@($parts | Where-Object { $excludedNames -contains $_ }).Count -gt 0) { continue }
        $inventory[$relative.Replace('/', '\')] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    return $inventory
}

function Get-InventoryDigest {
    param([hashtable]$Inventory)

    $lines = @($Inventory.Keys | Sort-Object | ForEach-Object { "$_=$($Inventory[$_])" })
    $payload = [System.Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
    $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash($payload)
    return (-join ($hash | ForEach-Object { $_.ToString("x2") }))
}

function Get-TargetDefinition {
    param([string]$Id)
    $definition = @($sourceConfig.generated_targets | Where-Object { $_.id -eq $Id }) | Select-Object -First 1
    if ($null -eq $definition) { throw "Target '$Id' is missing from plugin-source.json" }
    return $definition
}

function Compare-Target {
    param(
        [string]$Id,
        [string]$Path,
        [bool]$Required,
        [string]$Kind = "plugin"
    )

    $sourceInventory = Get-Inventory -Root $sourceRoot
    if (-not (Test-Path -LiteralPath $Path)) {
        return [ordered]@{
            id = $Id
            kind = $Kind
            path = $Path
            required = $Required
            status = "missing"
            source_file_count = $sourceInventory.Count
            target_file_count = 0
            source_digest = Get-InventoryDigest -Inventory $sourceInventory
            target_digest = $null
            missing = @($sourceInventory.Keys | Sort-Object)
            extra = @()
            mismatched = @()
        }
    }

    $targetInventory = Get-Inventory -Root $Path
    $missing = @($sourceInventory.Keys | Where-Object { -not $targetInventory.ContainsKey($_) } | Sort-Object)
    $extra = @($targetInventory.Keys | Where-Object { -not $sourceInventory.ContainsKey($_) } | Sort-Object)
    $mismatched = @($sourceInventory.Keys | Where-Object { $targetInventory.ContainsKey($_) -and $sourceInventory[$_] -ne $targetInventory[$_] } | Sort-Object)
    $status = if ($missing.Count -eq 0 -and $extra.Count -eq 0 -and $mismatched.Count -eq 0) { "pass" } else { "drift" }
    return [ordered]@{
        id = $Id
        kind = $Kind
        path = $Path
        required = $Required
        status = $status
        source_file_count = $sourceInventory.Count
        target_file_count = $targetInventory.Count
        source_digest = Get-InventoryDigest -Inventory $sourceInventory
        target_digest = Get-InventoryDigest -Inventory $targetInventory
        missing = $missing
        extra = $extra
        mismatched = $mismatched
    }
}

function Compare-Marketplace {
    param(
        [string]$Id,
        [string]$Path,
        [bool]$Required
    )

    $templateHash = (Get-FileHash -LiteralPath $marketplaceTemplate -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not (Test-Path -LiteralPath $Path)) {
        return [ordered]@{
            id = $Id
            kind = "marketplace"
            path = $Path
            required = $Required
            status = "missing"
            source_file_count = 1
            target_file_count = 0
            source_digest = $templateHash
            target_digest = $null
            missing = @("marketplace-template")
            extra = @()
            mismatched = @()
        }
    }
    $targetHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    $marketplaceMismatched = [string[]]@()
    if ($targetHash -ne $templateHash) {
        $marketplaceMismatched = [string[]]@("marketplace-template")
    }
    return [ordered]@{
        id = $Id
        kind = "marketplace"
        path = $Path
        required = $Required
        status = if ($targetHash -eq $templateHash) { "pass" } else { "drift" }
        source_file_count = 1
        target_file_count = 1
        source_digest = $templateHash
        target_digest = $targetHash
        missing = @()
        extra = @()
        mismatched = $marketplaceMismatched
    }
}

$workspaceTarget = Get-TargetDefinition -Id "workspace-marketplace"
$localTarget = Get-TargetDefinition -Id "codex-local"
$cacheTarget = Get-TargetDefinition -Id "codex-cache"
$workspaceRootConfigured = Join-Path $WorkspaceRoot ([string]$workspaceTarget.root)
$workspacePlugin = Join-Path $workspaceRootConfigured ([string]$workspaceTarget.plugin_path)
$workspaceMarketplace = Join-Path $workspaceRootConfigured ([string]$workspaceTarget.marketplace_path)
$localPlugin = Join-Path $UserProfileRoot (Join-Path ".codex\plugins\local" ([string]$localTarget.plugin_path))
$cachePlugin = Join-Path $UserProfileRoot ".codex\plugins\cache\bhm-marketplace\bhm-codex-connector\$([string]$sourceManifest.version)"

$sourceInventory = Get-Inventory -Root $sourceRoot
$targets = @(
    Compare-Target -Id "workspace-marketplace" -Path $workspacePlugin -Required $true
    Compare-Marketplace -Id "workspace-marketplace-manifest" -Path $workspaceMarketplace -Required $true
    Compare-Target -Id "codex-local" -Path $localPlugin -Required $true
    Compare-Target -Id "codex-cache" -Path $cachePlugin -Required ([bool]$RequireCache)
)

$requiredFailures = @($targets | Where-Object { $_.required -and $_.status -ne "pass" })
$warnings = @($targets | Where-Object { -not $_.required -and $_.status -ne "pass" })
$result = [ordered]@{
    ok = ($requiredFailures.Count -eq 0)
    plugin = [string]$sourceManifest.name
    version = [string]$sourceManifest.version
    source = $sourceRoot
    source_file_count = $sourceInventory.Count
    source_digest = Get-InventoryDigest -Inventory $sourceInventory
    cache_required = [bool]$RequireCache
    targets = $targets
    failures = $requiredFailures
    warnings = $warnings
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 20
} else {
    $result | ConvertTo-Json -Depth 20
}

if ($requiredFailures.Count -gt 0) { exit 1 }
exit 0
