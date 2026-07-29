[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ReleaseArchive,
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [string]$PythonPath = "",
    [string]$ReleaseNotesPath = "",
    [switch]$Force,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

function Resolve-Python {
    param([string]$Candidate)
    if (-not [string]::IsNullOrWhiteSpace($Candidate)) {
        return (Resolve-Path -LiteralPath $Candidate -ErrorAction Stop).Path
    }
    return (Get-Command python -ErrorAction Stop).Source
}

function Invoke-JsonScript {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $output = @(& $Python $Script @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Promotion preflight failed: $($output -join [Environment]::NewLine)"
    }
    return ($output -join [Environment]::NewLine | ConvertFrom-Json)
}

function Assert-OutputSafe {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path.TrimEnd('\', '/')
    if ($resolved -eq $repoRoot -or $resolved.StartsWith("$repoRoot\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Promotion output must stay outside the repository checkout: $resolved"
    }
    return $resolved
}

function Write-Utf8Json {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][object]$Value)
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 20) + "`n", [Text.UTF8Encoding]::new($false))
}

function Get-RelativeFiles {
    param([Parameter(Mandatory = $true)][string]$Root)
    @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force | ForEach-Object {
        [pscustomobject]@{
            path = $_.FullName.Substring($Root.Length).TrimStart('\', '/')
            size = $_.Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
        }
    })
}

$python = Resolve-Python -Candidate $PythonPath
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$archive = (Resolve-Path -LiteralPath $ReleaseArchive -ErrorAction Stop).Path
$sidecar = "$archive.sha256"
$verifyScript = Join-Path $repoRoot "scripts\verify-release-build.py"
$trustScript = Join-Path $repoRoot "scripts\verify-release-trust.py"
$notes = if ([string]::IsNullOrWhiteSpace($ReleaseNotesPath)) {
    Join-Path $repoRoot "docs\releases\bhm-v1.7.1-release-notes.md"
} else {
    (Resolve-Path -LiteralPath $ReleaseNotesPath -ErrorAction Stop).Path
}
if (-not (Test-Path -LiteralPath $sidecar)) { throw "Release SHA-256 sidecar is missing: $sidecar" }
if (-not (Test-Path -LiteralPath $notes)) { throw "Release notes are missing: $notes" }

$archiveVerification = Invoke-JsonScript -Python $python -Script $verifyScript -Arguments @(
    "--archive", $archive, "--expected-version", "v1.7.1"
)
$trustVerification = Invoke-JsonScript -Python $python -Script $trustScript -Arguments @(
    "--archive", $archive, "--expected-version", "v1.7.1"
)
$output = Assert-OutputSafe -Path $OutputRoot
$bundleRoot = Join-Path $output ("BHM-Promotion-v{0}" -f $archiveVerification.release_version)
if (Test-Path -LiteralPath $bundleRoot) {
    if (-not $Force) { throw "Promotion bundle already exists; use -Force: $bundleRoot" }
    Remove-Item -LiteralPath $bundleRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $bundleRoot | Out-Null

$archiveName = [IO.Path]::GetFileName($archive)
Copy-Item -LiteralPath $archive -Destination (Join-Path $bundleRoot $archiveName) -Force
Copy-Item -LiteralPath $sidecar -Destination (Join-Path $bundleRoot ([IO.Path]::GetFileName($sidecar))) -Force
Copy-Item -LiteralPath $notes -Destination (Join-Path $bundleRoot "RELEASE-NOTES.md") -Force

$archiveDigest = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $bundleRoot $archiveName)).Hash.ToLowerInvariant()
$sidecarDigest = ([regex]::Match((Get-Content -LiteralPath (Join-Path $bundleRoot ([IO.Path]::GetFileName($sidecar))) -Raw), '(?i)\b[a-f0-9]{64}\b')).Value.ToLowerInvariant()
$provenance = $trustVerification.artifacts | Where-Object { $_.path -eq "provenance.json" } | Select-Object -First 1
$manifest = [ordered]@{
    schema_version = 1
    product = "BlackHoleMemory"
    release_version = [string]$archiveVerification.release_version
    promotion_state = "prepared-not-published"
    source_revision = [string]$trustVerification.source_revision
    source_dirty = [bool]$trustVerification.source_dirty
    archive = [ordered]@{
        filename = $archiveName
        sha256 = $archiveDigest
        sidecar_filename = [IO.Path]::GetFileName($sidecar)
        sidecar_sha256 = $sidecarDigest
        sidecar_match = ($archiveDigest -eq $sidecarDigest)
    }
    trust = [ordered]@{
        mode = [string]$trustVerification.trust_mode
        signature_status = [string]$trustVerification.signature_status
        release_manifest_sha256 = [string]$trustVerification.release_manifest_sha256
        sbom_package_count = [int]$trustVerification.sbom_package_count
        provenance_sha256 = if ($null -ne $provenance) { [string]$provenance.sha256 } else { "" }
    }
    verification = [ordered]@{
        release_verifier = [bool]$archiveVerification.ok
        trust_verifier = [bool]$trustVerification.ok
        external_actions_performed = $false
    }
    artifacts = @(
        [ordered]@{ path = $archiveName; size = (Get-Item -LiteralPath (Join-Path $bundleRoot $archiveName)).Length; sha256 = $archiveDigest },
        [ordered]@{ path = [IO.Path]::GetFileName($sidecar); size = (Get-Item -LiteralPath (Join-Path $bundleRoot ([IO.Path]::GetFileName($sidecar)))).Length; sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $bundleRoot ([IO.Path]::GetFileName($sidecar)))).Hash.ToLowerInvariant() },
        [ordered]@{ path = "RELEASE-NOTES.md"; size = (Get-Item -LiteralPath (Join-Path $bundleRoot "RELEASE-NOTES.md")).Length; sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $bundleRoot "RELEASE-NOTES.md")).Hash.ToLowerInvariant() }
    )
}
$manifestPath = Join-Path $bundleRoot "promotion-manifest.json"
Write-Utf8Json -Path $manifestPath -Value $manifest
$result = [pscustomobject]@{
    ok = ([bool]$manifest.archive.sidecar_match -and [bool]$manifest.verification.release_verifier -and [bool]$manifest.verification.trust_verifier)
    action = "prepare-promotion"
    mutation = $true
    promotion_state = $manifest.promotion_state
    bundle_root = $bundleRoot
    archive = $manifest.archive
    trust = $manifest.trust
    external_actions_performed = $manifest.verification.external_actions_performed
    files = Get-RelativeFiles -Root $bundleRoot
    promotion_manifest = $manifestPath
}
if ($AsJson) { $result | ConvertTo-Json -Depth 20 } else { $result }
if ($result.ok) { exit 0 }
exit 1
