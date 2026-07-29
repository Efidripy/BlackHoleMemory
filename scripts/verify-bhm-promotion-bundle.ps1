[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundleRoot,
    [string]$PythonPath = "",
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
        throw "Promotion artifact verifier failed: $($output -join [Environment]::NewLine)"
    }
    return ($output -join [Environment]::NewLine | ConvertFrom-Json)
}

$root = (Resolve-Path -LiteralPath $BundleRoot -ErrorAction Stop).Path
$manifestPath = Join-Path $root "promotion-manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath)) { throw "Promotion manifest is missing: $manifestPath" }
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$failures = @()
if ($manifest.promotion_state -ne "prepared-not-published") { $failures += "promotion state is not prepared-not-published" }
if ([bool]$manifest.verification.external_actions_performed) { $failures += "external actions are marked as performed" }
if ([bool]$manifest.source_dirty) { $failures += "promotion source is marked dirty" }

foreach ($artifact in @($manifest.artifacts)) {
    $relative = [string]$artifact.path
    if ([string]::IsNullOrWhiteSpace($relative) -or [IO.Path]::IsPathRooted($relative) -or $relative.Contains('..')) {
        $failures += "unsafe promotion artifact path: $relative"
        continue
    }
    $path = Join-Path $root $relative
    if (-not (Test-Path -LiteralPath $path)) { $failures += "missing promotion artifact: $relative"; continue }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
    if ($actual -ne ([string]$artifact.sha256).ToLowerInvariant()) { $failures += "promotion artifact hash mismatch: $relative" }
    if ((Get-Item -LiteralPath $path).Length -ne [int64]$artifact.size) { $failures += "promotion artifact size mismatch: $relative" }
}

$python = Resolve-Python -Candidate $PythonPath
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$archivePath = Join-Path $root ([string]$manifest.archive.filename)
$verifyScript = Join-Path $repoRoot "scripts\verify-release-build.py"
$trustScript = Join-Path $repoRoot "scripts\verify-release-trust.py"
if (-not (Test-Path -LiteralPath $archivePath)) { $failures += "promotion archive is missing" }
$releaseVerification = $null
$trustVerification = $null
if (Test-Path -LiteralPath $archivePath) {
    $releaseVerification = Invoke-JsonScript -Python $python -Script $verifyScript -Arguments @("--archive", $archivePath, "--expected-version", ("v" + [string]$manifest.release_version))
    $trustVerification = Invoke-JsonScript -Python $python -Script $trustScript -Arguments @("--archive", $archivePath, "--expected-version", ("v" + [string]$manifest.release_version))
    if (-not [bool]$releaseVerification.ok) { $failures += "release verifier returned not-ok" }
    if (-not [bool]$trustVerification.ok) { $failures += "trust verifier returned not-ok" }
    if ([string]$trustVerification.source_revision -ne [string]$manifest.source_revision) { $failures += "source revision mismatch" }
}

$result = [pscustomobject]@{
    ok = ($failures.Count -eq 0)
    action = "verify-promotion"
    mutation = $false
    bundle_root = $root
    promotion_state = $manifest.promotion_state
    external_actions_performed = [bool]$manifest.verification.external_actions_performed
    source_revision = [string]$manifest.source_revision
    source_dirty = [bool]$manifest.source_dirty
    release_verifier = if ($null -ne $releaseVerification) { [bool]$releaseVerification.ok } else { $false }
    trust_verifier = if ($null -ne $trustVerification) { [bool]$trustVerification.ok } else { $false }
    failures = $failures
}
if ($AsJson) { $result | ConvertTo-Json -Depth 20 } else { $result }
if ($result.ok) { exit 0 }
exit 1
