param(
    [ValidateSet("verify", "rollback-plan")][string]$Action = "verify",
    [string]$ReleaseArchive = "E:\GitHub\workspace\local\tmp\bhm-releases\BHM-Release-v1.7.0.zip",
    [string]$BaseUrl = '',
    [string]$ExpectedSha256 = "",
    [switch]$RequireRuntimeSource,
    [switch]$RequireTrust,
    [string]$PythonPath = "",
    [switch]$KeepExtracted,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\runtime-endpoints.ps1')
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot (Split-Path -Parent $PSScriptRoot) }
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-ArchiveAudit {
    param([Parameter(Mandatory = $true)][string]$Path)

    $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entries = @()
        $unsafe = @()
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName.Replace('\', '/')
            $entries += $name
            if ($name.StartsWith('/') -or $name -match '^[A-Za-z]:/' -or $name.Split('/') -contains '..') {
                $unsafe += $name
            }
        }
        [pscustomobject]@{
            count = $entries.Count
            unsafe = $unsafe
            root = (@($entries | Where-Object { $_ -like 'BlackHoleMemory/*' }).Count -gt 0)
            entries = $entries
        }
    } finally {
        $archive.Dispose()
    }
}

function Get-LiveRuntimeAudit {
    param([Parameter(Mandatory = $true)][string]$Url)

    try {
        $health = Invoke-RestMethod -UseBasicParsing -Uri "$Url/bhm/health" -TimeoutSec 10
        $cutover = Invoke-RestMethod -UseBasicParsing -Uri "$Url/health/cutover" -TimeoutSec 10
        $slo = Invoke-RestMethod -UseBasicParsing -Uri "$Url/bhm/health/slo" -TimeoutSec 10
        [pscustomobject]@{
            ok = ($health.status -eq 'healthy' -and $health.memory_store.backend -eq 'sqlite-authoritative' -and [bool]$cutover.ok -and $slo.status -eq 'healthy')
            health = $health.status
            version = $health.version
            memory_store = $health.memory_store.backend
            cutover = [bool]$cutover.ok
            slo = $slo.status
            projection_pending = [int]$slo.observed.projection_pending
            projection_failed = [int]$slo.observed.projection_failed
            error = ""
        }
    } catch {
        [pscustomobject]@{ ok = $false; health = "unreachable"; version = ""; memory_store = ""; cutover = $false; slo = "unknown"; projection_pending = -1; projection_failed = -1; error = $_.Exception.Message }
    }
}

function Resolve-Python {
    param([string]$Candidate)
    if (-not [string]::IsNullOrWhiteSpace($Candidate)) {
        return (Resolve-Path -LiteralPath $Candidate -ErrorAction Stop).Path
    }
    return (Get-Command python -ErrorAction Stop).Source
}

function Invoke-TrustVerification {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string]$Archive
    )
    $output = @(& $Python $Script --archive (Resolve-Path -LiteralPath $Archive).Path --expected-version "v1.7.1" 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Release trust verifier failed: $($output -join [Environment]::NewLine)"
    }
    return ($output -join [Environment]::NewLine | ConvertFrom-Json)
}

if (-not (Test-Path -LiteralPath $ReleaseArchive)) {
    throw "Release archive not found: $ReleaseArchive"
}

$releaseHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ReleaseArchive).Hash.ToLowerInvariant()
$sidecar = "$ReleaseArchive.sha256"
$sidecarHash = ""
if (Test-Path -LiteralPath $sidecar) {
    $sidecarHash = ([regex]::Match((Get-Content -LiteralPath $sidecar -Raw), '(?i)\b[a-f0-9]{64}\b')).Value.ToLowerInvariant()
}
$expected = if ([string]::IsNullOrWhiteSpace($ExpectedSha256)) { $sidecarHash } else { $ExpectedSha256.ToLowerInvariant() }
$hashOk = (-not [string]::IsNullOrWhiteSpace($expected) -and $releaseHash -eq $expected)

$audit = Get-ArchiveAudit -Path $ReleaseArchive
$trust = $null
$trustVerifier = Join-Path $PSScriptRoot "verify-release-trust.py"
if ($RequireTrust) {
    $trust = Invoke-TrustVerification -Python (Resolve-Python -Candidate $PythonPath) -Script $trustVerifier -Archive $ReleaseArchive
}
$required = @(
    "BlackHoleMemory/release-manifest.json",
    "BlackHoleMemory/config/version-manifest.json",
    "BlackHoleMemory/BHM_Launcher.exe",
    "BlackHoleMemory/scripts/run-service.ps1",
    "BlackHoleMemory/scripts/start-bhm-workspace.ps1",
    "BlackHoleMemory/scripts/run-bhm-projection-worker.py",
    "BlackHoleMemory/scripts/verify-release-build.py"
)
if ($RequireTrust) {
    $required += @(
        "BlackHoleMemory/sbom.spdx.json",
        "BlackHoleMemory/provenance.json",
        "BlackHoleMemory/release-trust.json"
    )
}
if ($RequireRuntimeSource) {
    $required += @(
        "BlackHoleMemory/src/blackholememory/app.py",
        "BlackHoleMemory/src/blackholememory/version_manifest.py"
    )
}
$runtimeSourceFiles = @(
    "BlackHoleMemory/src/blackholememory/app.py",
    "BlackHoleMemory/src/blackholememory/version_manifest.py"
)
$missing = @($required | Where-Object { $audit.entries -notcontains $_ })
$runtimeSourceMissing = @($runtimeSourceFiles | Where-Object { $missing -contains $_ })
$p9NextRelease = @(
    "BlackHoleMemory/scripts/start-bhm-authoritative.ps1",
    "BlackHoleMemory/scripts/validate-bhm-streamable-http.ps1",
    "BlackHoleMemory/scripts/bhm-projection-operator.ps1"
)
$p9Missing = @($p9NextRelease | Where-Object { $audit.entries -notcontains $_ })
$runtime = Get-LiveRuntimeAudit -Url $BaseUrl

$tempRoot = Join-Path $env:TEMP ("bhm-release-postinstall-{0}" -f ([guid]::NewGuid().ToString('N')))
$extractRoot = Join-Path $tempRoot "BlackHoleMemory"
$manifest = $null
$versionManifest = $null
$extractionOk = $false
try {
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    [IO.Compression.ZipFile]::ExtractToDirectory($ReleaseArchive, $tempRoot)
    $extractionOk = Test-Path -LiteralPath $extractRoot
    if ($extractionOk) {
        $manifest = Get-Content (Join-Path $extractRoot "release-manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
        $versionManifest = Get-Content (Join-Path $extractRoot "config\version-manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    }
} finally {
    if (-not $KeepExtracted -and (Test-Path -LiteralPath $tempRoot)) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($Action -eq "rollback-plan") {
    $result = [pscustomobject]@{
        ok = $true
        action = $Action
        mutation = $false
        requires_operator_confirmation = $true
        archive = $ReleaseArchive
        archive_sha256 = $releaseHash
        rollback_surfaces = @(
            "runtime/live-memory/migration-backups",
            "runtime/live-memory/outbox-recovery",
            "workspace/local/tmp/bhm-releases"
        )
        note = "Plan only; no process stop, archive replacement, database restore or Qdrant mutation was performed."
    }
} else {
    $result = [pscustomobject]@{
        ok = ($hashOk -and $audit.root -and $audit.unsafe.Count -eq 0 -and $missing.Count -eq 0 -and $extractionOk -and $null -ne $manifest -and $null -ne $versionManifest -and $runtime.ok -and ($null -eq $trust -or [bool]$trust.ok))
        action = $Action
        archive = [pscustomobject]@{ path = $ReleaseArchive; sha256 = $releaseHash; expected_sha256 = $expected; hash_match = $hashOk; sidecar = $sidecar }
        archive_safety = [pscustomobject]@{ entries = $audit.count; unsafe = $audit.unsafe; missing_required = $missing; extracted = $extractionOk }
        runtime_source = [pscustomobject]@{ required = [bool]$RequireRuntimeSource; present = ($runtimeSourceMissing.Count -eq 0) }
        manifest = [pscustomobject]@{ release = $manifest.release_version; version = $versionManifest.release_version; p9_scripts_missing_from_frozen_archive = $p9Missing }
        trust = $trust
        runtime = $runtime
        note = "Post-install runtime probe targets the supplied BaseUrl; P9 scripts are recorded as next-release additions when absent from the frozen v1.7.0 archive."
    }
}

$result | ConvertTo-Json -Depth 12
if ($result.ok) { exit 0 }
exit 1
