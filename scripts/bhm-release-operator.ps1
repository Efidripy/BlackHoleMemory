[CmdletBinding()]
param(
    [ValidateSet("status", "install", "update", "rollback", "doctor", "native-attach")]
    [string]$Action = "status",
    [string]$ReleaseArchive = "",
    [string]$TargetRoot = "",
    [string]$BackupRoot = "",
    [string]$BaseUrl = '',
    [string]$PythonPath = "",
    [switch]$Confirm,
    [switch]$DryRun,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot }

function Get-BhmCallerHeaders {
    $token = [string]$env:BHM_CALLER_TOKEN
    if ([string]::IsNullOrWhiteSpace($token) -or $token.Trim().Length -lt 32) {
        $token = [string][Environment]::GetEnvironmentVariable('BHM_CALLER_TOKEN', 'User')
    }
    $token = $token.Trim()
    if ($token.Length -lt 32) {
        throw 'BHM_CALLER_TOKEN is unavailable'
    }
    return @{ Authorization = "Bearer $token" }
}

function Emit-Result {
    param([Parameter(Mandatory = $true)][object]$Value)
    if ($AsJson) {
        $Value | ConvertTo-Json -Depth 20
    }
    else {
        $Value
    }
}

function Resolve-Python {
    param([string]$Candidate)
    if (-not [string]::IsNullOrWhiteSpace($Candidate)) {
        return (Resolve-Path -LiteralPath $Candidate -ErrorAction Stop).Path
    }
    if (Test-Path -LiteralPath (Join-Path $repoRoot ".venv\Scripts\python.exe")) {
        return (Resolve-Path -LiteralPath (Join-Path $repoRoot ".venv\Scripts\python.exe")).Path
    }
    return (Get-Command python -ErrorAction Stop).Source
}

function Resolve-InstallRoot {
    if (-not [string]::IsNullOrWhiteSpace($TargetRoot)) {
        return [IO.Path]::GetFullPath($TargetRoot)
    }
    if ($env:BHM_INSTALL_ROOT) {
        return [IO.Path]::GetFullPath($env:BHM_INSTALL_ROOT)
    }
    return [IO.Path]::GetFullPath($repoRoot)
}

function Assert-TargetSafe {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [switch]$RequireExisting
    )
    $resolved = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $source = (Resolve-Path -LiteralPath $repoRoot).Path.TrimEnd('\', '/')
    if ($RequireExisting -and -not (Test-Path -LiteralPath $resolved)) {
        throw "Target root does not exist: $resolved"
    }
    if ((Test-Path -LiteralPath (Join-Path $resolved ".git")) -or $resolved -eq $source -or $resolved.StartsWith("$source\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to mutate a repository checkout: $resolved"
    }
    return $resolved
}

function Test-TargetProcesses {
    param([Parameter(Mandatory = $true)][string]$Root)
    try {
        $pattern = [regex]::Escape($Root)
        return @(Get-CimInstance Win32_Process | Where-Object {
            $commandLine = [string]$_.CommandLine
            $isRuntime = ($_.Name -match '(?i)^(python|pythonw|BHM_Launcher)\.exe$') -or
                $commandLine -match '(?i)(uvicorn|run-service\.ps1|start-bhm-authoritative\.ps1|BHM_Launcher\.exe)'
            $isRuntime -and $commandLine -match $pattern
        })
    }
    catch {
        throw "Unable to inspect target processes before mutation: $($_.Exception.Message)"
    }
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
        try { return ($reader.ReadToEnd() | ConvertFrom-Json) }
        finally { $reader.Dispose() }
    }
    finally { $archive.Dispose() }
}

function Verify-Archive {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Python
    )
    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $sidecar = "$resolved.sha256"
    if (-not (Test-Path -LiteralPath $sidecar)) {
        throw "Archive SHA-256 sidecar is missing: $sidecar"
    }
    $expectedHash = ([regex]::Match((Get-Content -LiteralPath $sidecar -Raw), '(?i)\b[a-f0-9]{64}\b')).Value.ToLowerInvariant()
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $resolved).Hash.ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($expectedHash) -or $expectedHash -ne $actualHash) {
        throw "Archive hash mismatch: $resolved"
    }
    $manifest = Get-ArchiveManifest -Path $resolved
    $version = [string]$manifest.release_version
    if ([string]::IsNullOrWhiteSpace($version)) {
        throw "Archive release version is empty: $resolved"
    }
    $verifyScript = Join-Path $repoRoot "scripts\verify-release-build.py"
    $trustVerifyScript = Join-Path $repoRoot "scripts\verify-release-trust.py"
    if ($version -eq "1.8.0" -and (Test-Path -LiteralPath $verifyScript)) {
        $output = @(& $Python $verifyScript --archive $resolved --expected-version ("v" + $version) 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "Release verifier rejected archive: $($output -join [Environment]::NewLine)"
        }
        if (-not (Test-Path -LiteralPath $trustVerifyScript)) {
            throw "Release trust verifier is missing: $trustVerifyScript"
        }
        $trustOutput = @(& $Python $trustVerifyScript --archive $resolved --expected-version ("v" + $version) 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "Release trust verifier rejected archive: $($trustOutput -join [Environment]::NewLine)"
        }
    }
    return [pscustomobject]@{
        path = $resolved
        version = $version
        sha256 = $actualHash
        manifest = $manifest
    }
}

function Get-RuntimeSnapshot {
    param([Parameter(Mandatory = $true)][string]$Url)
    try {
        $health = Invoke-RestMethod -UseBasicParsing -Uri "$Url/bhm/health" -TimeoutSec 8
        $cutover = Invoke-RestMethod -UseBasicParsing -Uri "$Url/health/cutover" -TimeoutSec 8
        $slo = Invoke-RestMethod -UseBasicParsing -Uri "$Url/bhm/health/slo" -TimeoutSec 8
        return [pscustomobject]@{
            reachable = $true
            ok = ($health.status -eq "healthy" -and $health.memory_store.backend -eq "sqlite-authoritative" -and [bool]$cutover.ok -and $slo.status -eq "healthy")
            health = $health.status
            version = $health.version
            memory_store = $health.memory_store.backend
            cutover = [bool]$cutover.ok
            slo = $slo.status
            projection_pending = [int]$slo.observed.projection_pending
            projection_failed = [int]$slo.observed.projection_failed
            error = ""
        }
    }
    catch {
        return [pscustomobject]@{
            reachable = $false
            ok = $false
            health = "unreachable"
            version = ""
            memory_store = ""
            cutover = $false
            slo = "unknown"
            projection_pending = -1
            projection_failed = -1
            error = $_.Exception.Message
        }
    }
}

function Get-AttachSnapshot {
    param([Parameter(Mandatory = $true)][string]$Url)
    try {
        $callerHeaders = Get-BhmCallerHeaders
        $contract = Invoke-RestMethod -UseBasicParsing -Uri "$Url/bhm/mcp/http/status" -Headers $callerHeaders -TimeoutSec 8
        $attach = $contract.sessions
        return [pscustomobject]@{
            status = [string]$attach.status
            attached_count = [int]$attach.attached_count
            pending_count = [int]$attach.pending_count
            expired_count = [int]$attach.expired_count
            servers = @($attach.sessions | Where-Object { $_.state -in @("catalog_ready", "healthy") } | ForEach-Object { [string]$_.client_id })
            authoritative_source = [string]$attach.authoritative_source
            error = ""
        }
    }
    catch {
        return [pscustomobject]@{ status = "unavailable"; attached_count = 0; pending_count = 0; expired_count = 0; servers = @(); authoritative_source = "streamable_http_sessions"; error = $_.Exception.Message }
    }
}

function Get-InstallSnapshot {
    param([Parameter(Mandatory = $true)][string]$Root)
    $manifestPath = Join-Path $Root "config\version-manifest.json"
    $manifest = $null
    if (Test-Path -LiteralPath $manifestPath) {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    [pscustomobject]@{
        root = $Root
        exists = (Test-Path -LiteralPath $Root)
        checkout_present = [bool](Test-Path -LiteralPath (Join-Path $Root ".git"))
        version = if ($null -ne $manifest) { [string]$manifest.release_version } else { "" }
        channel = if ($null -ne $manifest) { [string]$manifest.channel } else { "" }
        release_manifest = (Test-Path -LiteralPath (Join-Path $Root "release-manifest.json"))
        runtime_source = (Test-Path -LiteralPath (Join-Path $Root "src\blackholememory\app.py"))
        authoritative_initializer = (Test-Path -LiteralPath (Join-Path $Root "scripts\initialize-bhm-runtime.py"))
        runtime_dir = (Test-Path -LiteralPath (Join-Path $Root ".runtime"))
    }
}

function Copy-ExistingRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$StageRoot
    )
    $runtime = Join-Path $SourceRoot ".runtime"
    if (Test-Path -LiteralPath $runtime) {
        Copy-Item -LiteralPath $runtime -Destination $StageRoot -Recurse -Force
    }
}

function Invoke-Mutation {
    param(
        [ValidateSet("install", "update")][string]$Mode,
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][string]$Backup,
        [Parameter(Mandatory = $true)][string]$Python
    )
    if (-not $Confirm -and -not $DryRun) {
        throw "$Mode requires explicit -Confirm; use -DryRun for a non-mutating plan"
    }
    $archiveInfo = Verify-Archive -Path $Archive -Python $Python
    $targetInfo = Get-InstallSnapshot -Root $Target
    if ($Mode -eq "update" -and -not $targetInfo.exists) {
        throw "Update target does not exist: $Target"
    }
    if ($Backup -and (Test-Path -LiteralPath $Backup) -and -not $DryRun) {
        throw "Backup root already exists; choose a new path: $Backup"
    }
    if (@(Test-TargetProcesses -Root $Target).Count -gt 0 -and -not $DryRun) {
        throw "Target has running processes; stop the runtime before $Mode"
    }
    $plan = [pscustomobject]@{
        ok = $true
        action = $Mode
        mutation = $false
        archive = $archiveInfo
        target = $targetInfo
        backup_root = $Backup
        requires_confirmation = -not $DryRun
        note = if ($DryRun) { "Plan only; no target or backup mutation was performed." } else { "Target will be staged and replaced only after this explicit confirmation." }
    }
    if ($DryRun) { return $plan }
    $parent = Split-Path -Parent $Target
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $stageRoot = Join-Path $parent (".bhm-stage-{0}" -f ([guid]::NewGuid().ToString("N")))
    $extractRoot = Join-Path $parent (".bhm-extract-{0}" -f ([guid]::NewGuid().ToString("N")))
    try {
        New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
        [IO.Compression.ZipFile]::ExtractToDirectory($archiveInfo.path, $extractRoot)
        $extracted = Join-Path $extractRoot "BlackHoleMemory"
        if (-not (Test-Path -LiteralPath $extracted)) {
            throw "Archive did not extract BlackHoleMemory root"
        }
        Move-Item -LiteralPath $extracted -Destination $stageRoot -Force
        Remove-Item -LiteralPath $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
        if ($targetInfo.exists) {
            if ([string]::IsNullOrWhiteSpace($Backup)) { throw "$Mode requires -BackupRoot for an existing target" }
            New-Item -ItemType Directory -Path (Split-Path -Parent $Backup) -Force | Out-Null
            Copy-Item -LiteralPath $Target -Destination $Backup -Recurse -Force
            Copy-ExistingRuntime -SourceRoot $Target -StageRoot $stageRoot
            Remove-Item -LiteralPath $Target -Recurse -Force
        }
        Move-Item -LiteralPath $stageRoot -Destination $Target -Force
        $initializer = Join-Path $Target "scripts\initialize-bhm-runtime.py"
        if (-not (Test-Path -LiteralPath $initializer)) { throw "Installed bundle has no runtime initializer" }
    $initOutput = @(& $Python $initializer --runtime-dir (Join-Path $Target ".runtime") 2>&1)
        if ($LASTEXITCODE -ne 0) { throw "Installed runtime initialization failed: $($initOutput -join [Environment]::NewLine)" }
        return [pscustomobject]@{
            ok = $true
            action = $Mode
            mutation = $true
            archive = $archiveInfo
            target = Get-InstallSnapshot -Root $Target
            backup_root = $Backup
            runtime_initialized = $true
            note = "Archive activated after explicit confirmation; existing runtime was copied into the staged bundle."
        }
    }
    catch {
        if (Test-Path -LiteralPath $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $extractRoot) { Remove-Item -LiteralPath $extractRoot -Recurse -Force -ErrorAction SilentlyContinue }
        throw
    }
}

$target = Resolve-InstallRoot
$python = Resolve-Python -Candidate $PythonPath

switch ($Action) {
    "status" {
        Emit-Result ([pscustomobject]@{ ok = $true; action = $Action; mutation = $false; install = Get-InstallSnapshot -Root $target; runtime = Get-RuntimeSnapshot -Url $BaseUrl; attach = Get-AttachSnapshot -Url $BaseUrl })
        exit 0
    }
    "doctor" {
        $install = Get-InstallSnapshot -Root $target
        $runtime = Get-RuntimeSnapshot -Url $BaseUrl
        $attach = Get-AttachSnapshot -Url $BaseUrl
        $installContractOk = ($install.checkout_present -eq $false -and (
            $install.version -eq "" -or
            ($install.version -eq "1.8.0" -and $install.authoritative_initializer -and $install.runtime_source)
        ))
        $ok = ($runtime.ok -and $installContractOk)
        Emit-Result ([pscustomobject]@{ ok = $ok; action = $Action; mutation = $false; install = $install; runtime = $runtime; attach = $attach; note = "Doctor is read-only; Streamable HTTP session state is reported from the canonical runtime status." })
        if ($ok) { exit 0 }; exit 1
    }
    "native-attach" {
        $attach = Get-AttachSnapshot -Url $BaseUrl
        $runtime = Get-RuntimeSnapshot -Url $BaseUrl
        $ok = ($runtime.ok -and $attach.status -ne "unavailable")
        Emit-Result ([pscustomobject]@{ ok = $ok; action = $Action; mutation = $false; runtime = $runtime; attach = $attach; note = "No attach claim is inferred from config; only live Streamable HTTP session state is authoritative." })
        if ($ok) { exit 0 }; exit 1
    }
    "install" {
        $target = Assert-TargetSafe -Root $target
        Emit-Result (Invoke-Mutation -Mode install -Archive $ReleaseArchive -Target $target -Backup $BackupRoot -Python $python)
        exit 0
    }
    "update" {
        $target = Assert-TargetSafe -Root $target -RequireExisting
        Emit-Result (Invoke-Mutation -Mode update -Archive $ReleaseArchive -Target $target -Backup $BackupRoot -Python $python)
        exit 0
    }
    "rollback" {
        if (-not $Confirm -and -not $DryRun) { throw "rollback requires explicit -Confirm; use -DryRun for a non-mutating plan" }
        if ([string]::IsNullOrWhiteSpace($BackupRoot)) { throw "rollback requires -BackupRoot" }
        $target = Assert-TargetSafe -Root $target
        if (-not (Test-Path -LiteralPath $BackupRoot)) { throw "Backup root does not exist: $BackupRoot" }
        $plan = [pscustomobject]@{ ok = $true; action = $Action; mutation = $false; target = Get-InstallSnapshot -Root $target; backup = Get-InstallSnapshot -Root $BackupRoot; requires_confirmation = -not $DryRun }
        if ($DryRun) { Emit-Result $plan; exit 0 }
        if (@(Test-TargetProcesses -Root $target).Count -gt 0) { throw "Target has running processes; stop the runtime before rollback" }
        $failedCurrent = "$target.rollback-current-$([guid]::NewGuid().ToString('N'))"
        if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination $failedCurrent -Force }
        Copy-Item -LiteralPath $BackupRoot -Destination $target -Recurse -Force
        Emit-Result ([pscustomobject]@{ ok = $true; action = $Action; mutation = $true; target = Get-InstallSnapshot -Root $target; retained_current = $failedCurrent; note = "Rollback restored the explicit backup; the pre-rollback target was retained for operator cleanup." })
        exit 0
    }
}
