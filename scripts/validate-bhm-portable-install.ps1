param(
    [Parameter(Mandatory = $true)][string]$ReleaseArchive,
    [string]$ExpectedSourceRevision = "",
    [string]$PythonPath = "",
    [string]$QdrantUrl = '',
    [string]$QdrantCollection = "blackholememory",
    [ValidateRange(0, 65535)][int]$Port = 0,
    [ValidateRange(1, 60)][int]$CleanupTimeoutSeconds = 5,
    [switch]$KeepExtracted,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
if ([string]::IsNullOrWhiteSpace($QdrantUrl)) { $QdrantUrl = Get-BhmRuntimeEndpoint -Name 'qdrant_http' -RepoRoot $repoRoot }
$portableParts = Get-BhmRuntimeEndpointParts -Name 'portable_smoke' -RepoRoot $repoRoot
if ($Port -le 0) { $Port = $portableParts.Port }

function Resolve-Python {
    param([string]$Candidate)
    if (-not [string]::IsNullOrWhiteSpace($Candidate)) {
        $resolved = Resolve-Path -LiteralPath $Candidate -ErrorAction Stop
        return $resolved.Path
    }
    $command = Get-Command python -ErrorAction Stop
    return $command.Source
}

function Invoke-JsonScript {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $output = & $Python $Script @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Python gate failed ($LASTEXITCODE): $($output -join [Environment]::NewLine)"
    }
    return ($output -join [Environment]::NewLine | ConvertFrom-Json)
}

function Resolve-BhmCallerToken {
    $token = [string][Environment]::GetEnvironmentVariable('BHM_CALLER_TOKEN', 'Process')
    if ([string]::IsNullOrWhiteSpace($token)) {
        $token = [string][Environment]::GetEnvironmentVariable('BHM_CALLER_TOKEN', 'User')
    }
    if ([string]::IsNullOrWhiteSpace($token)) {
        $envPath = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.bhm\.env'
        foreach ($line in Get-Content -LiteralPath $envPath -ErrorAction SilentlyContinue) {
            if ($line -match '^\s*BHM_CALLER_TOKEN\s*=') {
                $token = $line.Split('=', 2)[1].Split('#', 2)[0].Trim().Trim('"').Trim("'")
                break
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace($token) -or $token.Trim().Length -lt 32) {
        throw 'BHM_CALLER_TOKEN is unavailable'
    }
    return $token.Trim()
}

function Get-JsonUrl {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][hashtable]$Headers
    )
    return Invoke-RestMethod -UseBasicParsing -Uri $Url -Headers $Headers -TimeoutSec 5
}

function Get-ArchiveReleaseVersion {
    param([Parameter(Mandatory = $true)][string]$Archive)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $entry = $zip.Entries | Where-Object {
            $_.FullName.Replace('\', '/') -eq 'BlackHoleMemory/config/version-manifest.json'
        } | Select-Object -First 1
        if ($null -eq $entry) { throw "Archive has no canonical version manifest: $Archive" }
        $reader = [IO.StreamReader]::new($entry.Open())
        try { return ([string](($reader.ReadToEnd() | ConvertFrom-Json).release_version)).Trim() }
        finally { $reader.Dispose() }
    }
    finally { $zip.Dispose() }
}

if (-not (Test-Path -LiteralPath $ReleaseArchive)) {
    throw "Release archive not found: $ReleaseArchive"
}
if ([string]::IsNullOrWhiteSpace($ExpectedSourceRevision)) {
    throw "-ExpectedSourceRevision is required; portable verification must bind the archive to an exact source revision"
}

$python = Resolve-Python -Candidate $PythonPath
$repoRoot = Split-Path -Parent $PSScriptRoot
$archivePath = (Resolve-Path -LiteralPath $ReleaseArchive).Path
$releaseVersion = Get-ArchiveReleaseVersion -Archive $archivePath
if ([string]::IsNullOrWhiteSpace($releaseVersion)) { throw "Archive release version is empty: $archivePath" }
$expectedVersion = "v$releaseVersion"
$verifyScript = Join-Path $repoRoot "scripts\verify-release-build.py"
$trustVerifyScript = Join-Path $repoRoot "scripts\verify-release-trust.py"
$tempRoot = Join-Path $env:TEMP ("bhm-portable-install-{0}" -f ([guid]::NewGuid().ToString("N")))
$bundleRoot = Join-Path $tempRoot "BlackHoleMemory"
$stdout = Join-Path $tempRoot "service-stdout.log"
$stderr = Join-Path $tempRoot "service-stderr.log"
$process = $null
$result = $null
$cleanupError = $null
$callerTokenWasPresent = Test-Path Env:BHM_CALLER_TOKEN
$previousCallerToken = [string]$env:BHM_CALLER_TOKEN
$callerToken = Resolve-BhmCallerToken
$callerHeaders = @{
    Authorization = "Bearer $callerToken"
    'X-BHM-Caller-Surface' = 'release-validator'
}

function Stop-PortableProcessBounded {
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Target,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )
    try { $Target.Refresh() } catch { return $true }
    if ($Target.HasExited) { return $true }
    Stop-Process -Id $Target.Id -Force -ErrorAction SilentlyContinue
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $Target.Refresh()
            if ($Target.HasExited) { return $true }
        } catch {
            return $true
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    Stop-Process -Id $Target.Id -Force -ErrorAction SilentlyContinue
    $retryDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $Target.Refresh()
            if ($Target.HasExited) { return $true }
        } catch {
            return $true
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $retryDeadline)
    return $false
}

try {
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

    $archiveVerification = Invoke-JsonScript -Python $python -Script $verifyScript -Arguments @(
        "--archive", $archivePath,
        "--expected-version", $expectedVersion
    )
    if (-not [bool]$archiveVerification.ok) {
        throw "Release archive verifier returned not-ok."
    }
    $trustVerification = Invoke-JsonScript -Python $python -Script $trustVerifyScript -Arguments @(
        "--archive", $archivePath,
        "--expected-version", $expectedVersion,
        "--expected-source-revision", $ExpectedSourceRevision
    )
    if (-not [bool]$trustVerification.ok) {
        throw "Release trust verifier returned not-ok."
    }

    Expand-Archive -LiteralPath (Resolve-Path -LiteralPath $ReleaseArchive).Path -DestinationPath $tempRoot -Force
    if (-not (Test-Path -LiteralPath $bundleRoot)) {
        throw "Archive did not extract a BlackHoleMemory root: $bundleRoot"
    }
    if (Test-Path -LiteralPath (Join-Path $bundleRoot ".git")) {
        throw "Portable bundle unexpectedly contains a .git checkout."
    }

    $initializer = Join-Path $bundleRoot "scripts\initialize-bhm-runtime.py"
    $sourceSentinels = @(
        (Join-Path $bundleRoot "src\blackholememory\app.py"),
        (Join-Path $bundleRoot "src\blackholememory\version_manifest.py"),
        $initializer
    )
    $missing = @($sourceSentinels | Where-Object { -not (Test-Path -LiteralPath $_) })
    if ($missing.Count -gt 0) {
        throw "Portable runtime source is incomplete: $($missing -join ', ')"
    }

    $runtimeRoot = Join-Path $bundleRoot "runtime"
    $init = Invoke-JsonScript -Python $python -Script $initializer -Arguments @(
        "--runtime-dir", $runtimeRoot
    )
    if (-not [bool]$init.ok) {
        throw "Portable runtime initialization failed."
    }

    $env:BHM_MEMORY_STORE_MODE = "sqlite-authoritative"
    $env:BHM_MEMORY_STORE_PARITY_CONFIRMED = "true"
    $env:BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED = "true"
    $env:BHM_FALLBACK_MODE = "explicit"
    $env:BHM_PROJECTION_WORKER_ENABLED = "false"
    $env:BHM_RUNTIME_DIR = $runtimeRoot
    $env:BHM_QDRANT_URL = $QdrantUrl
    $env:BHM_QDRANT_COLLECTION = $QdrantCollection
    $env:BHM_MEM0_ENABLED = "false"
    $env:BHM_CALLER_TOKEN = $callerToken
    # Portable-install smoke is intentionally provider-isolated.  The extracted
    # bundle must prove SQLite/cutover/Qdrant wiring without inheriting a host
    # LLM endpoint or waiting forever for a provider that is not part of the
    # release archive.  Live provider readiness is covered by the authoritative
    # runtime and semantic-fusion gates.
    $env:BHM_PROVIDER_WARMUP_DISABLED = "true"
    $env:PYTHONPATH = Join-Path $bundleRoot "src"

    $process = Start-Process -FilePath $python -ArgumentList @(
        "-m", "uvicorn", "blackholememory.app:app", "--host", "127.0.0.1", "--port", [string]$Port, "--log-level", "warning"
    ) -WorkingDirectory $bundleRoot -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru

    $deadline = (Get-Date).AddSeconds(45)
    $health = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        try {
            $health = Get-JsonUrl -Url "http://127.0.0.1:$Port/bhm/health" -Headers $callerHeaders
            break
        } catch {
            if ($process.HasExited) {
                break
            }
        }
    }
    if ($null -eq $health) {
        $logs = (@(Get-Content $stdout -ErrorAction SilentlyContinue) + @(Get-Content $stderr -ErrorAction SilentlyContinue)) -join [Environment]::NewLine
        throw "Portable runtime did not become reachable. $logs"
    }
    $cutover = $null
    $slo = $null
    $sloDeadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $sloDeadline) {
        $cutover = Get-JsonUrl -Url "http://127.0.0.1:$Port/health/cutover" -Headers $callerHeaders
        $slo = Get-JsonUrl -Url "http://127.0.0.1:$Port/bhm/health/slo" -Headers $callerHeaders
        if ([bool]$cutover.ok -and $slo.status -eq "healthy") { break }
        Start-Sleep -Milliseconds 500
    }
    $authoritative = (
        $health.status -eq "healthy" -and
        $health.version -eq "bhm-v$releaseVersion-PURE" -and
        $health.memory_store.backend -eq "sqlite-authoritative" -and
        [bool]$cutover.ok -and
        $slo.status -eq "healthy"
    )
    $result = [pscustomobject]@{
        ok = $authoritative
        archive = (Resolve-Path -LiteralPath $ReleaseArchive).Path
        archive_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $ReleaseArchive).Hash.ToLowerInvariant()
        trust = $trustVerification
        extracted_root = $bundleRoot
        checkout_present = [bool](Test-Path -LiteralPath (Join-Path $bundleRoot ".git"))
        runtime_source = [bool]($missing.Count -eq 0)
        initializer = $init
        health = $health.status
        version = $health.version
        memory_store = $health.memory_store.backend
        cutover = [bool]$cutover.ok
        slo = $slo.status
        projection_pending = [int]$slo.observed.projection_pending
        projection_failed = [int]$slo.observed.projection_failed
        python = $python
        port = $Port
        note = "Runtime process used only the extracted bundle as cwd/PYTHONPATH; host Python supplied dependencies."
    }
} finally {
    if ($process) {
        try {
            if (-not (Stop-PortableProcessBounded -Target $process -TimeoutSeconds $CleanupTimeoutSeconds)) {
                $cleanupError = "Portable runtime process cleanup exceeded bounded deadline of $CleanupTimeoutSeconds seconds. PID: $($process.Id)"
            }
        } catch {
            $cleanupError = $_.Exception.Message
        }
    }
    foreach ($name in @("BHM_MEMORY_STORE_MODE", "BHM_MEMORY_STORE_PARITY_CONFIRMED", "BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED", "BHM_FALLBACK_MODE", "BHM_PROJECTION_WORKER_ENABLED", "BHM_RUNTIME_DIR", "BHM_QDRANT_URL", "BHM_QDRANT_COLLECTION", "BHM_MEM0_ENABLED", "BHM_PROVIDER_WARMUP_DISABLED", "PYTHONPATH")) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    if ($callerTokenWasPresent) {
        $env:BHM_CALLER_TOKEN = $previousCallerToken
    } else {
        Remove-Item Env:BHM_CALLER_TOKEN -ErrorAction SilentlyContinue
    }
    if (-not $KeepExtracted -and (Test-Path -LiteralPath $tempRoot)) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($cleanupError) {
    throw $cleanupError
}
if ($null -eq $result) {
    throw "Portable install validator produced no result."
}
$result | ConvertTo-Json -Depth 12
if ($result.ok) { exit 0 }
exit 1
