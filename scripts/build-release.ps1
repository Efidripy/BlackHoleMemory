param(
  [string]$Version = "v1.8.0",
  [string]$PythonPath = "",
  [string]$OutputRoot = "",
  [switch]$RefreshCanonicalLauncher,
  [switch]$SignRelease,
  [string]$SigningKeyPath = "",
  [string]$SignerId = "",
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

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $repoRoot

$seedPython = if ([string]::IsNullOrWhiteSpace($PythonPath)) { "python" } else { (Resolve-Path $PythonPath).Path }
$launcherPy = Join-Path $repoRoot "scripts\bhm_launcher.py"
$iconPath = Join-Path $repoRoot "assets\bhm-control-panel.ico"
$distDir = Join-Path $repoRoot "dist"
$buildDir = Join-Path $repoRoot "build"
$artifactRoot = if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $repoRoot
} else {
    $resolvedOutputRoot = [IO.Path]::GetFullPath($OutputRoot)
    New-Item -ItemType Directory -Path $resolvedOutputRoot -Force | Out-Null
    $resolvedOutputRoot
}
$releaseRoot = Join-Path $artifactRoot "release_build"
$releaseDir = Join-Path $releaseRoot "BlackHoleMemory"
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
$releaseSignScript = Join-Path $repoRoot "scripts\sign-release-ed25519.py"
$releaseSignatureVerifyScript = Join-Path $repoRoot "scripts\verify-release-signature.py"
$sourceBoundaryScript = Join-Path $repoRoot "scripts\verify-local-source-boundary.ps1"

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
Assert-Path -Path $captureInputsScript -Message "Missing build-input evidence capture script: $captureInputsScript"
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
$sourceStatus = (& git -C $repoRoot status --porcelain --untracked-files=no 2>$null | Out-String).Trim()
$env:BHM_SOURCE_DIRTY = if ([string]::IsNullOrWhiteSpace($sourceStatus)) { "false" } else { "true" }

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uvCommand) {
    throw "uv is required for a trusted release build."
}
$previousProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
$releaseEnv = Join-Path ([IO.Path]::GetTempPath()) ("bhm-release-" + [guid]::NewGuid().ToString("N"))
$releasePython = Join-Path $releaseEnv "Scripts\python.exe"
trap {
    if ($null -eq $previousProjectEnvironment) {
        Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
    } else {
        $env:UV_PROJECT_ENVIRONMENT = $previousProjectEnvironment
    }
    if (Test-Path -LiteralPath $releaseEnv) {
        Remove-Item -LiteralPath $releaseEnv -Recurse -Force -ErrorAction SilentlyContinue
    }
    throw
}

Write-Step "Creating disposable locked release environment"
& uv venv --python $seedPython $releaseEnv
if ($LASTEXITCODE -ne 0) {
    throw "Unable to create disposable release environment."
}
$env:UV_PROJECT_ENVIRONMENT = $releaseEnv
& uv sync --locked --extra build --project $repoRoot
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
    "--add-data", ((Join-Path $repoRoot "assets") + ";assets"),
    "--add-data", ((Join-Path $repoRoot "scripts") + ";scripts"),
    "--add-data", ((Join-Path $repoRoot "plugins") + ";plugins"),
    "--add-data", ((Join-Path $repoRoot "infra") + ";infra"),
    "--add-data", ((Join-Path $repoRoot "config") + ";config"),
    "--add-data", ((Join-Path $repoRoot "pyproject.toml") + ";."),
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
        "--add-data", ((Join-Path $repoRoot "assets") + ";assets"),
        "--add-data", ((Join-Path $repoRoot "scripts") + ";scripts"),
        "--add-data", ((Join-Path $repoRoot "plugins") + ";plugins"),
        "--add-data", ((Join-Path $repoRoot "infra") + ";infra"),
        "--add-data", ((Join-Path $repoRoot "config") + ";config"),
        "--add-data", ((Join-Path $repoRoot "pyproject.toml") + ";."),
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
# Release builds are hermetic by default: PyInstaller output is copied into
# the bundle but does not mutate the tracked source tree after provenance is
# captured.  Operators may explicitly refresh the desktop launcher when they
# intend to create a separate binary commit.
if ($RefreshCanonicalLauncher) {
    Copy-Item -LiteralPath $compiledExe -Destination $canonicalExe -Force
}
Move-Item -LiteralPath $compiledExe -Destination $releaseExe -Force

$foldersToCopy = @("assets", "scripts", "plugins", "infra", "config", "src")
foreach ($folder in $foldersToCopy) {
    $source = Join-Path $repoRoot $folder
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
Copy-Item -LiteralPath (Join-Path $repoRoot "pyproject.toml") -Destination (Join-Path $releaseDir "pyproject.toml") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "uv.lock") -Destination (Join-Path $releaseDir "uv.lock") -Force
Copy-Item -LiteralPath $licensePath -Destination (Join-Path $releaseDir "LICENSE") -Force

Write-Step "Capturing consumed build-input evidence"
$uvVersion = (& uv --version | Out-String).Trim()
& $venvPython $captureInputsScript --root $releaseDir --output (Join-Path $releaseDir "build-inputs.json") --source-revision $sourceRevision --launcher $releaseExe --uv-version $uvVersion
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
& $venvPython $releaseTrustVerifyScript --release-root $releaseDir --expected-version $Version
if ($LASTEXITCODE -ne 0) {
    throw "Staged release trust verification failed."
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
& $venvPython $releaseTrustVerifyScript --archive $zipPath --expected-version $Version --sidecar $releaseHashPath
if ($LASTEXITCODE -ne 0) {
    throw "Release archive trust verification failed."
}
if ($SignRelease) {
    Write-Step "Creating detached Ed25519 release signature"
    & $venvPython $releaseSignScript --archive $zipPath --private-key $SigningKeyPath --expected-version $Version --signer-id $SignerId --authority $SignatureAuthority --source-revision $sourceRevision
    if ($LASTEXITCODE -ne 0) {
        throw "Detached Ed25519 release signing failed."
    }
    & $venvPython $releaseSignatureVerifyScript --archive $zipPath --signature "$zipPath.sig" --public-key "$zipPath.pub" --receipt "$zipPath.trust.json" --expected-version $Version
    if ($LASTEXITCODE -ne 0) {
        throw "Detached Ed25519 release signature verification failed."
    }
}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $sourceBoundaryScript -RepoRoot $repoRoot -StagingRoot $releaseDir -ArchivePath $zipPath
if ($LASTEXITCODE -ne 0) {
    throw "Local source boundary validation failed for release artifact."
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
if (Test-Path -LiteralPath $releaseEnv) {
    Remove-Item -LiteralPath $releaseEnv -Recurse -Force
}

$zipItem = Get-Item -LiteralPath $zipPath
Write-Host ""
Write-Host "Release archive created:" -ForegroundColor Green
Write-Host $zipItem.FullName
Write-Host ("Size: {0:N2} MB" -f ($zipItem.Length / 1MB))
Write-Host "SHA-256: $zipHash"
