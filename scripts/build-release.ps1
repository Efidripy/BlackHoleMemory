param(
  [string]$Version = "v1.8.1",
  [string]$PythonPath = "",
  [string]$OutputRoot = "",
  [switch]$RefreshCanonicalLauncher,
  [switch]$SkipCanonicalLauncherRefresh,
  [switch]$SignRelease,
  [string]$SigningKeyPath = "",
  [string]$SignerId = "",
  [string]$SignerTrustRegistry = "",
  [ValidateSet("operator", "external")][string]$SignatureAuthority = "operator"
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Assert-Path {
    param(
        [string]$Path,
        [string]$Message
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        throw $Message
    }
}

function Resolve-SafeArtifactRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$AllowCheckoutRoot
    )
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $checkout = (Resolve-Path -LiteralPath $repoRoot).Path.TrimEnd('\', '/')
    if (-not $AllowCheckoutRoot -and ($full -eq $checkout -or $full.StartsWith("$checkout\", [StringComparison]::OrdinalIgnoreCase))) {
        throw "Custom OutputRoot must remain outside the repository checkout: $full"
    }
    $current = [IO.DirectoryInfo]::new($full)
    while ($null -ne $current) {
        if (Test-Path -LiteralPath $current.FullName) {
            try {
                $item = Get-Item -LiteralPath $current.FullName -Force -ErrorAction Stop
            }
            catch {
                throw "Unable to inspect artifact root component: $($current.FullName)"
            }
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing reparse-point artifact root component: $($current.FullName)"
            }
            if ($item -is [IO.FileInfo]) {
                throw "Artifact root is an existing file: $($current.FullName)"
            }
        }
        $parent = $current.Parent
        if ($null -eq $parent -or $parent.FullName -eq $current.FullName) { break }
        $current = $parent
    }
    return $full
}

function Assert-DisposablePathSafe {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $full)) { return }
    try {
        $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Refusing to clean reparse disposable path: $full"
        }
        if ($item -is [IO.DirectoryInfo]) {
            foreach ($child in @(Get-ChildItem -LiteralPath $full -Recurse -Force -ErrorAction Stop)) {
                if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                    throw "Refusing to clean disposable tree containing reparse entry: $($child.FullName)"
                }
            }
        }
    }
    catch {
        if ($_.Exception.Message -like "Refusing to clean disposable*") { throw }
        throw "Unable to inspect disposable cleanup path: $full"
    }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $repoRoot

$seedPython = if ([string]::IsNullOrWhiteSpace($PythonPath)) { "python" } else { (Resolve-Path $PythonPath).Path }
$launcherPy = Join-Path $repoRoot "scripts\bhm_launcher.py"
$iconPath = Join-Path $repoRoot "assets\bhm-control-panel.ico"
$artifactRoot = if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $repoRoot
} else {
    $resolvedOutputRoot = [IO.Path]::GetFullPath($OutputRoot)
    $resolvedOutputRoot
}
$artifactRoot = Resolve-SafeArtifactRoot -Path $artifactRoot -AllowCheckoutRoot:([string]::IsNullOrWhiteSpace($OutputRoot))
if (-not [string]::IsNullOrWhiteSpace($OutputRoot)) {
    New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null
}
$releaseRoot = Join-Path $artifactRoot "release_build"
$releaseDir = Join-Path $releaseRoot "BlackHoleMemory"
# Keep compiler intermediates inside the run-scoped disposable root.  Never
# clean a shared checkout-level .dist/.build directory on failure.
$distDir = Join-Path $releaseRoot "dist"
$buildDir = Join-Path $releaseRoot "build"
$exeName = "BHM_Launcher.exe"
$compiledExe = Join-Path $distDir $exeName
$canonicalExe = Join-Path $repoRoot $exeName
$releaseExe = Join-Path $releaseDir $exeName
$zipPath = Join-Path $artifactRoot "BHM-Release-$Version.zip"
$releaseHashPath = Join-Path $artifactRoot "BHM-Release-$Version.zip.sha256"
$versionManifestPath = Join-Path $repoRoot "config\version-manifest.json"
$licensePath = Join-Path $repoRoot "LICENSE"
$releaseManifestScript = Join-Path $repoRoot "scripts\build-release-manifest.py"
$releaseTrustScript = Join-Path $repoRoot "scripts\build-release-trust.py"
$releaseVerifyScript = Join-Path $repoRoot "scripts\verify-release-build.py"
$releaseTrustVerifyScript = Join-Path $repoRoot "scripts\verify-release-trust.py"
$captureInputsScript = Join-Path $repoRoot "scripts\capture-release-build-inputs.py"
$sourceMaterializeScript = Join-Path $repoRoot "scripts\materialize-release-source.py"
$releaseSignScript = Join-Path $repoRoot "scripts\sign-release-ed25519.py"
$releaseSignatureVerifyScript = Join-Path $repoRoot "scripts\verify-release-signature.py"
$sourceTreeVerifyScript = Join-Path $repoRoot "scripts\verify-release-source-tree.py"
$defaultSignerTrustRegistry = Join-Path $repoRoot "config\release-signer-trust.json"
$sourceBoundaryScript = Join-Path $repoRoot "scripts\verify-local-source-boundary.ps1"
# Initialize cleanup state before any pre-flight assertion can throw.  The
# trusted-build trap must preserve the original failure (for example, a dirty
# source tree) instead of masking it with a null-path cleanup error.
$releaseEnv = $null
$sourceSnapshotRoot = $null
$previousPythonPath = $null

Write-Step "Pre-flight validation"
if (-not [string]::IsNullOrWhiteSpace($PythonPath)) {
    Assert-Path -Path $seedPython -Message "Missing seed Python interpreter: $seedPython"
}
Assert-Path -Path $launcherPy -Message "Missing launcher script: $launcherPy"
Assert-Path -Path $versionManifestPath -Message "Missing canonical version manifest: $versionManifestPath"
Assert-Path -Path $licensePath -Message "Missing root project license: $licensePath"
Assert-Path -Path $releaseManifestScript -Message "Missing release manifest builder: $releaseManifestScript"
Assert-Path -Path $releaseTrustScript -Message "Missing release trust builder: $releaseTrustScript"
Assert-Path -Path $releaseVerifyScript -Message "Missing release verifier: $releaseVerifyScript"
Assert-Path -Path $releaseTrustVerifyScript -Message "Missing release trust verifier: $releaseTrustVerifyScript"
Assert-Path -Path $sourceTreeVerifyScript -Message "Missing source-tree verifier: $sourceTreeVerifyScript"
Assert-Path -Path $captureInputsScript -Message "Missing build-input evidence capture script: $captureInputsScript"
Assert-Path -Path $sourceMaterializeScript -Message "Missing immutable source materializer: $sourceMaterializeScript"
if ($SignRelease) {
    Assert-Path -Path $releaseSignScript -Message "Missing detached signature tool: $releaseSignScript"
    Assert-Path -Path $releaseSignatureVerifyScript -Message "Missing detached signature verifier: $releaseSignatureVerifyScript"
    if ([string]::IsNullOrWhiteSpace($SigningKeyPath)) {
        throw "-SignRelease requires -SigningKeyPath pointing to an operator-controlled key outside the repository."
    }
    Assert-Path -Path $SigningKeyPath -Message "Missing signing key: $SigningKeyPath"
    if ([string]::IsNullOrWhiteSpace($SignerId)) {
        throw "-SignRelease requires -SignerId so the trust receipt is attributable."
    }
    $signerTrustRegistry = if ([string]::IsNullOrWhiteSpace($SignerTrustRegistry)) { $defaultSignerTrustRegistry } else { (Resolve-Path $SignerTrustRegistry).Path }
    Assert-Path -Path $signerTrustRegistry -Message "Missing pinned signer trust registry: $signerTrustRegistry"
}
if ($RefreshCanonicalLauncher -and $SkipCanonicalLauncherRefresh) {
    throw "-RefreshCanonicalLauncher and -SkipCanonicalLauncherRefresh cannot be used together."
}
Assert-Path -Path $sourceBoundaryScript -Message "Missing local source boundary verifier: $sourceBoundaryScript"
$boundaryProbe = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $sourceBoundaryScript -RepoRoot $repoRoot
if ($LASTEXITCODE -ne 0) {
    throw "Local source boundary validation failed: $boundaryProbe"
}
$versionManifest = Get-Content -Raw -LiteralPath $versionManifestPath -Encoding UTF8 | ConvertFrom-Json
$expectedVersion = "v$([string]$versionManifest.release_version)"
if ($Version -ne $expectedVersion) {
    throw "Release version '$Version' does not match canonical version '$expectedVersion'."
}
$sourceRevision = (& git -C $repoRoot rev-parse HEAD 2>$null | Select-Object -First 1).ToString().Trim()
if ($sourceRevision -notmatch '^[0-9a-fA-F]{40}$') {
    throw "Unable to resolve source revision for release provenance."
}
$env:BHM_SOURCE_REVISION = $sourceRevision
# Ignore unreadable untracked test caches when collecting provenance; tracked
# modifications remain fully visible and are still marked as a dirty source.
$sourceStatus = (& git -C $repoRoot status --porcelain --untracked-files=all 2>$null | Out-String).Trim()
if (-not [string]::IsNullOrWhiteSpace($sourceStatus)) {
    throw "Trusted release build requires an exact clean tracked tree; commit or remove local changes first."
}
$sourceTree = (& git -C $repoRoot rev-parse 'HEAD^{tree}' 2>$null | Select-Object -First 1).ToString().Trim()
if ($sourceTree -notmatch '^[0-9a-fA-F]{40}$') {
    throw "Unable to resolve canonical source tree for release provenance."
}
$env:BHM_SOURCE_DIRTY = "false"
$env:BHM_SOURCE_TREE = $sourceTree

# The seed interpreter runs before the disposable locked environment exists.
# Make the tracked source package importable in a fresh checkout without
# relying on a globally installed project or caller-specific environment.
$previousPythonPath = $env:PYTHONPATH
$sourcePackageRoot = Join-Path $repoRoot "src"
$env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
    $sourcePackageRoot
} else {
    "$sourcePackageRoot$([IO.Path]::PathSeparator)$previousPythonPath"
}

Write-Step "Pre-flighting exact tracked source tree"
$sourceOnlyProbe = & $seedPython $sourceTreeVerifyScript --source-root $repoRoot --expected-revision $sourceRevision --expected-tree $sourceTree --check-source-only | Out-String
if ($LASTEXITCODE -ne 0) {
    throw "Exact tracked source-tree preflight failed: $sourceOnlyProbe"
}
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uvCommand) {
    throw "uv is required for a trusted release build."
}
$previousProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
$releaseEnv = Join-Path ([IO.Path]::GetTempPath()) ("bhm-release-" + [guid]::NewGuid().ToString("N"))
$releasePython = Join-Path $releaseEnv "Scripts\python.exe"
trap {
    $failure = $_
    if ($null -eq $previousProjectEnvironment) {
        Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
    } else {
        $env:UV_PROJECT_ENVIRONMENT = $previousProjectEnvironment
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$releaseEnv) -and (Test-Path -LiteralPath $releaseEnv)) {
        Remove-Item -LiteralPath $releaseEnv -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$sourceSnapshotRoot) -and (Test-Path -LiteralPath $sourceSnapshotRoot)) {
        Remove-Item -LiteralPath $sourceSnapshotRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    foreach ($disposablePath in @($releaseRoot, $buildDir, $distDir, $zipPath, $releaseHashPath)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$disposablePath) -and (Test-Path -LiteralPath $disposablePath)) {
            Remove-Item -LiteralPath $disposablePath -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    # Re-throw the captured exception explicitly.  A bare `throw` from a
    # cleanup trap can surface as the generic `ScriptHalted` and hide the
    # actionable pre-flight reason (for example, a dirty tracked tree).
    if ($null -ne $failure -and $null -ne $failure.Exception) {
        throw $failure.Exception
    }
    throw $failure
}

Write-Step "Creating disposable locked release environment"
& uv venv --python $seedPython $releaseEnv
if ($LASTEXITCODE -ne 0) {
    throw "Unable to create disposable release environment."
}
$env:UV_PROJECT_ENVIRONMENT = $releaseEnv
& uv sync --locked --no-install-project --extra build --project $repoRoot
if ($LASTEXITCODE -ne 0) {
    throw "Locked release environment synchronization failed."
}
if (-not (Test-Path -LiteralPath $releasePython)) {
    throw "Disposable release interpreter was not created: $releasePython"
}
& $releasePython -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is missing after locked release synchronization."
}
$venvPython = $releasePython

Write-Step "Cleaning previous release artifacts"
Assert-DisposablePathSafe -Path $releaseRoot
Assert-DisposablePathSafe -Path $zipPath
Assert-DisposablePathSafe -Path $releaseHashPath
if (Test-Path -LiteralPath $releaseRoot) {
    Remove-Item -LiteralPath $releaseRoot -Recurse -Force
}
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
if (Test-Path -LiteralPath $releaseHashPath) {
    Remove-Item -LiteralPath $releaseHashPath -Force
}
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

Write-Step "Materializing immutable tracked source snapshot"
$sourceSnapshotRoot = Join-Path ([IO.Path]::GetTempPath()) ("bhm-source-" + [guid]::NewGuid().ToString("N"))
$sourceSnapshotJson = & $seedPython $sourceMaterializeScript --repo-root $repoRoot --output-root $sourceSnapshotRoot --expected-revision $sourceRevision --expected-tree $sourceTree | Out-String
if ($LASTEXITCODE -ne 0) {
    throw "Immutable tracked source materialization failed: $sourceSnapshotJson"
}
$sourceSnapshotResult = $sourceSnapshotJson | ConvertFrom-Json
if ([string]$sourceSnapshotResult.source_snapshot_sha256 -notmatch '^[0-9a-fA-F]{64}$') {
    throw "Immutable source snapshot digest is missing or invalid."
}
$sourceRoot = $sourceSnapshotRoot
$launcherPy = Join-Path $sourceRoot "scripts\bhm_launcher.py"
$iconPath = Join-Path $sourceRoot "assets\bhm-control-panel.ico"
$licensePath = Join-Path $sourceRoot "LICENSE"

Write-Step "Compiling launcher with PyInstaller"
$pyInstallerArgs = @(
    "-m", "PyInstaller",
    "--onefile",
    "--noconsole",
    "--clean",
    "--name", "BHM_Launcher",
    "--specpath", (Join-Path $buildDir "spec"),
    "--workpath", (Join-Path $buildDir "work"),
    "--distpath", $distDir,
    "--add-data", ((Join-Path $sourceRoot "assets") + ";assets"),
    "--add-data", ((Join-Path $sourceRoot "scripts") + ";scripts"),
    "--add-data", ((Join-Path $sourceRoot "plugins") + ";plugins"),
    "--add-data", ((Join-Path $sourceRoot "infra") + ";infra"),
    "--add-data", ((Join-Path $sourceRoot "config") + ";config"),
    "--add-data", ((Join-Path $sourceRoot "pyproject.toml") + ";."),
    "--add-data", ($licensePath + ";."),
    $launcherPy
)

if (Test-Path -LiteralPath $iconPath) {
    $pyInstallerArgs = @(
        "-m", "PyInstaller",
        "--onefile",
        "--noconsole",
        "--clean",
        "--name", "BHM_Launcher",
        "--icon", $iconPath,
        "--specpath", (Join-Path $buildDir "spec"),
        "--workpath", (Join-Path $buildDir "work"),
        "--distpath", $distDir,
        "--add-data", ((Join-Path $sourceRoot "assets") + ";assets"),
        "--add-data", ((Join-Path $sourceRoot "scripts") + ";scripts"),
        "--add-data", ((Join-Path $sourceRoot "plugins") + ";plugins"),
        "--add-data", ((Join-Path $sourceRoot "infra") + ";infra"),
        "--add-data", ((Join-Path $sourceRoot "config") + ";config"),
        "--add-data", ((Join-Path $sourceRoot "pyproject.toml") + ";."),
        "--add-data", ($licensePath + ";."),
        $launcherPy
    )
}

& $venvPython @pyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}
Assert-Path -Path $compiledExe -Message "PyInstaller did not produce expected executable: $compiledExe"

Write-Step "Assembling clean distribution folder"
# The canonical desktop launcher is a local root artifact. It is refreshed only
# after every release gate passes and only when explicitly requested. A normal
# trusted build therefore remains non-mutating with respect to the checkout.
Move-Item -LiteralPath $compiledExe -Destination $releaseExe -Force

$foldersToCopy = @("assets", "scripts", "plugins", "infra", "config", "src")
foreach ($folder in $foldersToCopy) {
    $source = Join-Path $sourceRoot $folder
    $destination = Join-Path $releaseDir $folder
    Assert-Path -Path $source -Message "Missing required runtime folder: $source"
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    foreach ($file in Get-ChildItem -LiteralPath $source -Recurse -File -Force) {
        if ($file.FullName -match '[\\/]__pycache__[\\/]' -or $file.Extension -eq ".pyc") {
            continue
        }
        $relative = $file.FullName.Substring($source.Length).TrimStart('\', '/')
        $target = Join-Path $destination $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $target -Force
    }
}
Copy-Item -LiteralPath (Join-Path $sourceRoot "pyproject.toml") -Destination (Join-Path $releaseDir "pyproject.toml") -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot "uv.lock") -Destination (Join-Path $releaseDir "uv.lock") -Force
Copy-Item -LiteralPath $licensePath -Destination (Join-Path $releaseDir "LICENSE") -Force

Write-Step "Verifying exact tracked source snapshot"
$sourceTreeProbeJson = & $venvPython $sourceTreeVerifyScript --source-root $repoRoot --release-root $releaseDir --expected-revision $sourceRevision --expected-tree $sourceTree | Out-String
if ($LASTEXITCODE -ne 0) {
    throw "Initial staged source-tree snapshot verification failed: $sourceTreeProbeJson"
}
$sourceTreeProbe = $sourceTreeProbeJson | ConvertFrom-Json
$sourceSnapshot = [string]$sourceTreeProbe.source_snapshot_sha256
if ($sourceSnapshot -notmatch '^[0-9a-fA-F]{64}$' -or $sourceSnapshot -ne [string]$sourceTreeProbe.staged_snapshot_sha256 -or $sourceSnapshot -ne [string]$sourceSnapshotResult.source_snapshot_sha256) {
    throw "Initial staged source-tree snapshot digest is missing or mismatched."
}

Write-Step "Capturing consumed build-input evidence"
$uvVersion = (& uv --version | Out-String).Trim()
& $venvPython $captureInputsScript --root $releaseDir --output (Join-Path $releaseDir "build-inputs.json") --source-revision $sourceRevision --source-snapshot-sha256 $sourceSnapshot --launcher $releaseExe --uv-version $uvVersion
if ($LASTEXITCODE -ne 0) {
    throw "Build-input evidence capture failed."
}

Write-Step "Generating and verifying release manifest"
& $venvPython $releaseManifestScript --root $releaseDir --expected-version $Version
if ($LASTEXITCODE -ne 0) {
    throw "Release manifest generation failed."
}
& $venvPython $releaseTrustScript --root $releaseDir --expected-version $Version
if ($LASTEXITCODE -ne 0) {
    throw "Release trust metadata generation failed."
}
& $venvPython $releaseVerifyScript --release-root $releaseDir --expected-version $Version
if ($LASTEXITCODE -ne 0) {
    throw "Staged release verification failed."
}
& $venvPython $releaseTrustVerifyScript --release-root $releaseDir --expected-version $Version --expected-source-revision $sourceRevision
if ($LASTEXITCODE -ne 0) {
    throw "Staged release trust verification failed."
}
Write-Step "Verifying staged source-tree snapshot"
& $venvPython $sourceTreeVerifyScript --source-root $repoRoot --release-root $releaseDir --expected-revision $sourceRevision --expected-tree $sourceTree
if ($LASTEXITCODE -ne 0) {
    throw "Staged source-tree snapshot verification failed."
}

Write-Step "Generating release archive"
Compress-Archive -Path $releaseDir -DestinationPath $zipPath -Force
Assert-Path -Path $zipPath -Message "Release archive was not created: $zipPath"
& $venvPython $releaseVerifyScript --archive $zipPath --expected-version $Version
if ($LASTEXITCODE -ne 0) {
    throw "Release archive verification failed."
}
$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText($releaseHashPath, "$zipHash *$([IO.Path]::GetFileName($zipPath))`n", [System.Text.UTF8Encoding]::new($false))
& $venvPython $releaseTrustVerifyScript --archive $zipPath --expected-version $Version --sidecar $releaseHashPath --expected-source-revision $sourceRevision
if ($LASTEXITCODE -ne 0) {
    throw "Release archive trust verification failed."
}
& $venvPython $sourceTreeVerifyScript --source-root $repoRoot --release-root $releaseDir --expected-revision $sourceRevision --expected-tree $sourceTree
if ($LASTEXITCODE -ne 0) {
    throw "Pre-sign source-tree snapshot verification failed."
}
if ($SignRelease) {
    Write-Step "Creating detached Ed25519 release signature"
    & $venvPython $releaseSignScript --archive $zipPath --private-key $SigningKeyPath --expected-version $Version --signer-id $SignerId --authority $SignatureAuthority --source-revision $sourceRevision --repository-root $repoRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Detached Ed25519 release signing failed."
    }
    & $venvPython $releaseSignatureVerifyScript --archive $zipPath --signature "$zipPath.sig" --public-key "$zipPath.pub" --receipt "$zipPath.trust.json" --expected-version $Version --trust-registry $signerTrustRegistry --expected-source-revision $sourceRevision
    if ($LASTEXITCODE -ne 0) {
        throw "Detached Ed25519 release signature verification failed."
    }
}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $sourceBoundaryScript -RepoRoot $repoRoot -StagingRoot $releaseDir -ArchivePath $zipPath
if ($LASTEXITCODE -ne 0) {
    throw "Local source boundary validation failed for release artifact."
}

if ($RefreshCanonicalLauncher) {
    Write-Step "Refreshing canonical desktop launcher"
    Copy-Item -LiteralPath $releaseExe -Destination $canonicalExe -Force
}

Write-Step "Cleaning temporary build folders"
if (Test-Path -LiteralPath $releaseRoot) {
    Remove-Item -LiteralPath $releaseRoot -Recurse -Force
}
if (Test-Path -LiteralPath $buildDir) {
    Remove-Item -LiteralPath $buildDir -Recurse -Force
}
if ($null -eq $previousProjectEnvironment) {
    Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
} else {
    $env:UV_PROJECT_ENVIRONMENT = $previousProjectEnvironment
}
if ($null -eq $previousPythonPath) {
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
} else {
    $env:PYTHONPATH = $previousPythonPath
}
if (Test-Path -LiteralPath $releaseEnv) {
    Remove-Item -LiteralPath $releaseEnv -Recurse -Force
}
if (Test-Path -LiteralPath $sourceSnapshotRoot) {
    Remove-Item -LiteralPath $sourceSnapshotRoot -Recurse -Force
}

$zipItem = Get-Item -LiteralPath $zipPath
Write-Host ""
Write-Host "Release archive created:" -ForegroundColor Green
Write-Host $zipItem.FullName
Write-Host ("Size: {0:N2} MB" -f ($zipItem.Length / 1MB))
Write-Host "SHA-256: $zipHash"
