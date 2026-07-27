param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$StagingRoot = "",
    [string]$ArchivePath = ""
)

$ErrorActionPreference = "Stop"

$repo = (Resolve-Path -LiteralPath $RepoRoot).Path
$failures = [System.Collections.Generic.List[string]]::new()

function Add-Failure {
    param([string]$Message)
    $failures.Add($Message)
}

function Read-Text {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return @()
    }
    return @(Get-Content -LiteralPath $Path -Encoding UTF8)
}

$gitignore = Read-Text (Join-Path $repo ".gitignore")
if (-not ($gitignore | Where-Object { $_.Trim() -eq ".src/" })) {
    Add-Failure "root .gitignore does not contain .src/"
}

$dockerignore = Read-Text (Join-Path $repo ".dockerignore")
if (-not ($dockerignore | Where-Object { $_.Trim() -eq ".src/" })) {
    Add-Failure "root .dockerignore does not contain .src/"
}

Push-Location $repo
try {
    $tracked = @(& git ls-files --cached -- .src 2>$null)
    if ($tracked.Count -gt 0) {
        Add-Failure ("tracked .src paths found: " + ($tracked -join ", "))
    }
    $staged = @(& git diff --cached --name-only -- .src 2>$null)
    if ($staged.Count -gt 0) {
        Add-Failure ("staged .src paths found: " + ($staged -join ", "))
    }
    $visibleUntracked = @(& git ls-files --others --exclude-standard -- .src 2>$null)
    if ($visibleUntracked.Count -gt 0) {
        Add-Failure ("non-ignored .src paths found: " + ($visibleUntracked -join ", "))
    }
} catch {
    Add-Failure ("git boundary probe failed: " + $_.Exception.Message)
} finally {
    Pop-Location
}

function Find-SourceBoundaryResidue {
    param([string]$Root)
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return @()
    }
    $rootPath = (Resolve-Path -LiteralPath $Root).Path
    $residue = @()
    foreach ($item in Get-ChildItem -LiteralPath $rootPath -Recurse -Force) {
        $relative = $item.FullName.Substring($rootPath.Length).TrimStart('\', '/')
        if (($relative -split '[\\/]' | Where-Object { $_ -eq ".src" }).Count -gt 0) {
            $residue += $item.FullName
        }
    }
    return $residue
}

if (-not [string]::IsNullOrWhiteSpace($StagingRoot)) {
    $stagingResidue = Find-SourceBoundaryResidue -Root $StagingRoot
    if ($stagingResidue.Count -gt 0) {
        Add-Failure ("staging contains .src residue: " + ($stagingResidue -join ", "))
    }
}

if (-not [string]::IsNullOrWhiteSpace($ArchivePath)) {
    if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
        Add-Failure "archive does not exist: $ArchivePath"
    } else {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = $null
        try {
            $archive = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path -LiteralPath $ArchivePath).Path)
            $entries = @(
                $archive.Entries |
                    Where-Object { $_.FullName -match '(^|/)\.src(/|$)' } |
                    Select-Object -ExpandProperty FullName
            )
            if ($entries.Count -gt 0) {
                Add-Failure ("archive contains .src entries: " + ($entries -join ", "))
            }
        } catch {
            Add-Failure ("archive boundary probe failed: " + $_.Exception.Message)
        } finally {
            if ($archive) {
                $archive.Dispose()
            }
        }
    }
}

$result = [pscustomobject]@{
    ok = ($failures.Count -eq 0)
    repo_root = $repo
    source_root = (Join-Path $repo ".src")
    staging_checked = (-not [string]::IsNullOrWhiteSpace($StagingRoot))
    archive_checked = (-not [string]::IsNullOrWhiteSpace($ArchivePath))
    failures = @($failures)
}

$result | ConvertTo-Json -Depth 4
if (-not $result.ok) {
    exit 1
}
