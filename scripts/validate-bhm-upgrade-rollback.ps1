param(
    [Parameter(Mandatory = $true)][string]$FromArchive,
    [Parameter(Mandatory = $true)][string]$ToArchive,
    [string]$PythonPath = "",
    [string]$QdrantCatalogUrl = '',
    [switch]$KeepWorkdir,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
if ([string]::IsNullOrWhiteSpace($QdrantCatalogUrl)) { $QdrantCatalogUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot -Path 'bhm/telemetry/qdrant-catalog' }

function Resolve-Python {
    param([string]$Candidate)
    if (-not [string]::IsNullOrWhiteSpace($Candidate)) {
        return (Resolve-Path -LiteralPath $Candidate -ErrorAction Stop).Path
    }
    return (Get-Command python -ErrorAction Stop).Source
}

function Get-ArchiveManifest {
    param([Parameter(Mandatory = $true)][string]$Path)
    $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entry = $archive.Entries | Where-Object {
            $_.FullName.Replace('\', '/') -eq "BlackHoleMemory/config/version-manifest.json"
        } | Select-Object -First 1
        if ($null -eq $entry) {
            throw "Archive has no canonical version manifest: $Path"
        }
        $reader = [IO.StreamReader]::new($entry.Open())
        try {
            return ($reader.ReadToEnd() | ConvertFrom-Json)
        } finally {
            $reader.Dispose()
        }
    } finally {
        $archive.Dispose()
    }
}

function Verify-ArchiveHash {
    param([Parameter(Mandatory = $true)][string]$Path)
    $sidecar = "$Path.sha256"
    if (-not (Test-Path -LiteralPath $sidecar)) {
        throw "Missing archive SHA-256 sidecar: $sidecar"
    }
    $expected = ([regex]::Match((Get-Content -LiteralPath $sidecar -Raw), '(?i)\b[a-f0-9]{64}\b')).Value.ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($expected) -or $expected -ne $actual) {
        throw "Archive hash mismatch: $Path (expected=$expected actual=$actual)"
    }
    return $actual
}

function Get-TreeDigest {
    param([Parameter(Mandatory = $true)][string]$Root)
    $resolvedRoot = (Resolve-Path -LiteralPath $Root -ErrorAction Stop).Path
    $lines = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $resolvedRoot -Recurse -File -Force | Sort-Object FullName)) {
        $relative = $file.FullName.Substring($resolvedRoot.Length).TrimStart('\', '/') -replace '\\', '/'
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
        $lines += "$relative|$($file.Length)|$hash"
    }
    $payload = [Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
    $digest = [Security.Cryptography.SHA256]::Create().ComputeHash($payload)
    return [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
}

function Extract-Bundle {
    param(
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    [IO.Compression.ZipFile]::ExtractToDirectory($Archive, $Destination)
    $bundle = Join-Path $Destination "BlackHoleMemory"
    if (-not (Test-Path -LiteralPath $bundle)) {
        throw "Archive did not extract a BlackHoleMemory root: $Archive"
    }
    return $bundle
}

function Copy-Bundle {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Recurse -Force
}

function Read-InstalledVersion {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $path = Join-Path $InstallRoot "config\version-manifest.json"
    return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json).release_version.ToString()
}

if (-not (Test-Path -LiteralPath $FromArchive)) {
    throw "From archive not found: $FromArchive"
}
if (-not (Test-Path -LiteralPath $ToArchive)) {
    throw "To archive not found: $ToArchive"
}

$python = Resolve-Python -Candidate $PythonPath
$repoRoot = Split-Path -Parent $PSScriptRoot
$initializer = Join-Path $repoRoot "scripts\initialize-bhm-runtime.py"
$workRoot = Join-Path $env:TEMP ("bhm-upgrade-rollback-{0}" -f ([guid]::NewGuid().ToString("N")))
$fromExtract = Join-Path $workRoot "from"
$toExtract = Join-Path $workRoot "to"
$installRoot = Join-Path $workRoot "install\BlackHoleMemory"
$backupRoot = Join-Path $workRoot "backup\BlackHoleMemory"
$result = $null

try {
    New-Item -ItemType Directory -Path $workRoot -Force | Out-Null
    $fromPath = (Resolve-Path -LiteralPath $FromArchive).Path
    $toPath = (Resolve-Path -LiteralPath $ToArchive).Path
    $fromHash = Verify-ArchiveHash -Path $fromPath
    $toHash = Verify-ArchiveHash -Path $toPath
    $fromManifest = Get-ArchiveManifest -Path $fromPath
    $toManifest = Get-ArchiveManifest -Path $toPath
    if ($fromManifest.release_version.ToString() -ne "1.7.0") {
        throw "From archive must be v1.7.0, got $($fromManifest.release_version)"
    }
    if ($toManifest.release_version.ToString() -ne "1.8.0") {
        throw "To archive must be v1.8.0, got $($toManifest.release_version)"
    }

    $fromBundle = Extract-Bundle -Archive $fromPath -Destination $fromExtract
    $toBundle = Extract-Bundle -Archive $toPath -Destination $toExtract
    Copy-Bundle -Source $fromBundle -Destination $installRoot

    if (-not (Test-Path -LiteralPath $initializer)) {
        throw "Missing runtime initializer in source checkout: $initializer"
    }
    $runtimeRoot = Join-Path $installRoot "runtime"
    $runtimeConfigRoot = Join-Path $runtimeRoot "config"
    New-Item -ItemType Directory -Path $runtimeConfigRoot -Force | Out-Null
    [pscustomobject]@{
        memory_store_mode = "sqlite-authoritative"
        qdrant_projection_only = $true
        release_channel = "PURE"
        upgrade_sentinel = "p14.2"
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $runtimeConfigRoot "runtime-config.json") -Encoding UTF8

    $initOutput = @(& $python $initializer --runtime-dir $runtimeRoot 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Runtime initializer failed: $($initOutput -join [Environment]::NewLine)"
    }
    $qdrantSnapshot = Invoke-RestMethod -UseBasicParsing -Uri $QdrantCatalogUrl -TimeoutSec 15
    $qdrantSnapshotPath = Join-Path $runtimeRoot "qdrant-catalog.json"
    $qdrantSnapshot | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $qdrantSnapshotPath -Encoding UTF8

    $stateBefore = Get-TreeDigest -Root $runtimeRoot
    Copy-Bundle -Source $installRoot -Destination $backupRoot
    foreach ($entry in @(Get-ChildItem -LiteralPath $toBundle -Force)) {
        Copy-Item -LiteralPath $entry.FullName -Destination $installRoot -Recurse -Force
    }

    $upgradeVersion = Read-InstalledVersion -InstallRoot $installRoot
    $upgradeSourceComplete = Test-Path -LiteralPath (Join-Path $installRoot "src\blackholememory\app.py")
    $stateAfterUpgrade = Get-TreeDigest -Root $runtimeRoot
    $upgradeStatePreserved = $stateBefore -eq $stateAfterUpgrade

    $tamperTarget = Join-Path $installRoot "src\blackholememory\app.py"
    if (-not (Test-Path -LiteralPath $tamperTarget)) {
        throw "Upgrade target missing runtime source before rollback injection."
    }
    Remove-Item -LiteralPath $tamperTarget -Force
    $failureDetected = -not (Test-Path -LiteralPath $tamperTarget)
    Copy-Bundle -Source $backupRoot -Destination $installRoot

    $rollbackVersion = Read-InstalledVersion -InstallRoot $installRoot
    $stateAfterRollback = Get-TreeDigest -Root $runtimeRoot
    $rollbackStatePreserved = $stateBefore -eq $stateAfterRollback
    $rollbackSourceMatchesFrozen = -not (Test-Path -LiteralPath (Join-Path $installRoot "src"))
    $rollbackOk = $failureDetected -and $rollbackVersion -eq "1.7.0" -and $rollbackStatePreserved -and $rollbackSourceMatchesFrozen

    $result = [pscustomobject]@{
        ok = ($fromHash -and $toHash -and $upgradeVersion -eq "1.8.0" -and $upgradeSourceComplete -and $upgradeStatePreserved -and $rollbackOk)
        workdir = $workRoot
        from_archive = [pscustomobject]@{ path = $fromPath; version = $fromManifest.release_version; sha256 = $fromHash }
        to_archive = [pscustomobject]@{ path = $toPath; version = $toManifest.release_version; sha256 = $toHash }
        qdrant_catalog_snapshot = [pscustomobject]@{ path = $qdrantSnapshotPath; digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $qdrantSnapshotPath).Hash.ToLowerInvariant(); read_only = $true }
        sqlite_runtime = [pscustomobject]@{ digest_before = $stateBefore; digest_after_upgrade = $stateAfterUpgrade; digest_after_rollback = $stateAfterRollback; preserved_upgrade = $upgradeStatePreserved; preserved_rollback = $rollbackStatePreserved }
        upgrade = [pscustomobject]@{ version = $upgradeVersion; runtime_source_complete = $upgradeSourceComplete; state_preserved = $upgradeStatePreserved }
        rollback = [pscustomobject]@{ failure_injected = $failureDetected; version = $rollbackVersion; state_preserved = $rollbackStatePreserved; frozen_shape_restored = $rollbackSourceMatchesFrozen }
        mutation = $false
        note = "Upgrade and rollback were executed only in a temporary extracted installation; live SQLite/Qdrant were not mutated."
    }
} finally {
    if (-not $KeepWorkdir -and (Test-Path -LiteralPath $workRoot)) {
        Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($null -eq $result) {
    throw "Upgrade/rollback drill produced no result."
}
$result | ConvertTo-Json -Depth 12
if ($result.ok) { exit 0 }
exit 1
