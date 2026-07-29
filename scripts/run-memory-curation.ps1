param(
    [string]$AuxAuditReport = "",
    [string]$ExcludeProjectRegex = "^(bhm-surface-smoke-|smoke-|noise-smoke-|next20-smoke$)",
    [int]$LessonLimit = 12,
    [switch]$ApplySafe
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing JSON file: $Path"
    }

    $raw = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $null
    }
    return $raw | ConvertFrom-Json
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )

    $json = $Value | ConvertTo-Json -Depth 20
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json, $utf8NoBom)
}

function Normalize-Key {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ""
    }
    return (($Value.ToLowerInvariant() -replace "\s+", " ").Trim())
}

$runtimeDir = Join-Path $PSScriptRoot "..\.runtime\live-memory"
$reportDir = Join-Path $PSScriptRoot "..\.runtime\refinery"
$runId = "{0:yyyy-MM-dd_HH-mm-ss}" -f (Get-Date)
$mode = if ($ApplySafe) { "apply-safe" } else { "audit-only" }
$reportPath = Join-Path $reportDir "memory-curation-$mode-$runId.json"
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null

if ([string]::IsNullOrWhiteSpace($AuxAuditReport)) {
$latest = Get-ChildItem (Join-Path $PSScriptRoot "..\.runtime\refinery\memory-aux-audit-*.json") |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $latest) {
        throw "No memory-aux-audit report found"
    }
    $AuxAuditReport = $latest.FullName
}

$audit = Read-JsonFile -Path $AuxAuditReport
$checkpoints = Read-JsonFile -Path (Join-Path $runtimeDir "checkpoints.json")
$projectMaps = Read-JsonFile -Path (Join-Path $runtimeDir "project-maps.json")
$adrs = Read-JsonFile -Path (Join-Path $runtimeDir "adrs.json")
$handoffs = Read-JsonFile -Path (Join-Path $runtimeDir "handoffs.json")
$sessionRecords = Read-JsonFile -Path (Join-Path $runtimeDir "session-records.json")
$validationSnapshots = Read-JsonFile -Path (Join-Path $runtimeDir "validation-snapshots.json")
$lessons = Read-JsonFile -Path (Join-Path $runtimeDir "lessons.json")
if ($null -eq $lessons) {
    $lessons = @()
}

$storeMap = @{
    "checkpoints" = [object[]]$checkpoints
    "project_maps" = [object[]]$projectMaps
    "adrs" = [object[]]$adrs
    "handoffs" = [object[]]$handoffs
    "session_records" = [object[]]$sessionRecords
    "validation_snapshots" = [object[]]$validationSnapshots
}

$artifactCleanup = New-Object 'System.Collections.Generic.List[object]'
foreach ($artifact in @($audit.artifacts)) {
    $artifactType = [string]$artifact.artifact_type
    if (-not $storeMap.ContainsKey($artifactType)) {
        continue
    }

    $orphanIds = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($orphan in @($artifact.orphan_memory_refs)) {
        $project = [string]$orphan.project
        if ($project -match $ExcludeProjectRegex) {
            $orphanIds.Add([string]$orphan.id) | Out-Null
        }
    }

    if ($orphanIds.Count -eq 0) {
        continue
    }

    $before = @($storeMap[$artifactType])
    $after = @($before | Where-Object { -not $orphanIds.Contains([string]$_.id) })
    $removed = @($before | Where-Object { $orphanIds.Contains([string]$_.id) })

    if ($ApplySafe) {
        switch ($artifactType) {
            "checkpoints" { Write-JsonFile -Path (Join-Path $runtimeDir "checkpoints.json") -Value $after }
            "project_maps" { Write-JsonFile -Path (Join-Path $runtimeDir "project-maps.json") -Value $after }
            "adrs" { Write-JsonFile -Path (Join-Path $runtimeDir "adrs.json") -Value $after }
            "handoffs" { Write-JsonFile -Path (Join-Path $runtimeDir "handoffs.json") -Value $after }
            "session_records" { Write-JsonFile -Path (Join-Path $runtimeDir "session-records.json") -Value $after }
            "validation_snapshots" { Write-JsonFile -Path (Join-Path $runtimeDir "validation-snapshots.json") -Value $after }
        }
        $storeMap[$artifactType] = $after
    }

    $artifactCleanup.Add([ordered]@{
        artifact_type = $artifactType
        removed_count = @($removed).Count
        removed_ids = @($removed | ForEach-Object { $_.id })
        affected_projects = @($removed | ForEach-Object { $_.project } | Sort-Object -Unique)
    }) | Out-Null
}

$existingLessonKeys = New-Object 'System.Collections.Generic.HashSet[string]'
foreach ($lesson in @($lessons)) {
    $existingLessonKeys.Add((Normalize-Key -Value ([string]$lesson.content))) | Out-Null
}

$rankedCandidates = New-Object 'System.Collections.Generic.List[object]'
foreach ($candidate in @($audit.lessons.candidates)) {
    $project = [string]$candidate.project
    if ($project -match $ExcludeProjectRegex) {
        continue
    }

    $summaryText = [string]$candidate.summary
    $title = [string]$candidate.title
    $tags = @($candidate.concepts)
    $type = [string]$candidate.memory_type
    $score = 0

    if ($type -eq "bug") { $score += 40 } else { $score += 20 }
    $score += [Math]::Min(@($tags).Count, 8)
    if ($project -eq "e-github-workspace" -or $project -eq "agent-ops") { $score += 8 }
    if ($project -eq "multiserversubgen") { $score -= 2 }
    if ($title -match "checkpoint|hybrid session record") {
        continue
    }
    if ($summaryText -match "checkpoint:") {
        continue
    }
    if (-not [string]::IsNullOrWhiteSpace($summaryText) -and $summaryText.Length -gt 140) { $score += 5 }

    $lessonContent = "${project} / ${type}: $summaryText"
    $dedupeKey = Normalize-Key -Value $lessonContent
    if ($existingLessonKeys.Contains($dedupeKey)) {
        continue
    }

    $rankedCandidates.Add([ordered]@{
        source_id = $candidate.source_id
        project = $project
        memory_type = $type
        title = $title
        summary = $summaryText
        tags = $tags
        score = $score
        lesson_content = $lessonContent
        lesson_context = "source_id=$($candidate.source_id); title=$title"
    }) | Out-Null
}

$selectedLessons = @()
$seenProjectType = New-Object 'System.Collections.Generic.HashSet[string]'
$selectedLessonKeys = New-Object 'System.Collections.Generic.HashSet[string]'
foreach ($candidate in @($rankedCandidates | Sort-Object -Property @(
    @{ Expression = { $_.score }; Descending = $true },
    @{ Expression = { $_.project } },
    @{ Expression = { $_.title } }
))) {
    if (@($selectedLessons).Count -ge $LessonLimit) {
        break
    }
    $bucket = "$($candidate.project)|$($candidate.memory_type)"
    if ($seenProjectType.Contains($bucket) -and $candidate.project -eq "multiserversubgen") {
        continue
    }
    if ($selectedLessonKeys.Contains((Normalize-Key -Value $candidate.lesson_content))) {
        continue
    }
    $selectedLessons += $candidate
    $seenProjectType.Add($bucket) | Out-Null
    $selectedLessonKeys.Add((Normalize-Key -Value $candidate.lesson_content)) | Out-Null
    $existingLessonKeys.Add((Normalize-Key -Value $candidate.lesson_content)) | Out-Null
}

$createdLessons = New-Object 'System.Collections.Generic.List[object]'
if ($ApplySafe) {
    foreach ($candidate in @($selectedLessons)) {
        $lesson = [ordered]@{
            id = "lesson_bhm_$([guid]::NewGuid().ToString('N').Substring(0,16))"
            content = $candidate.lesson_content
            context = $candidate.lesson_context
            confidence = if ($candidate.memory_type -eq "bug") { 0.9 } else { 0.82 }
            project = $candidate.project
            tags = @("auto-curated", "lesson", $candidate.memory_type) + @($candidate.tags | Select-Object -First 6)
            created_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
        }
        $lessons += ,$lesson
        $createdLessons.Add($lesson) | Out-Null
    }
    Write-JsonFile -Path (Join-Path $runtimeDir "lessons.json") -Value $lessons
}

$generatedAtJson = ((Get-Date).ToString("s") | ConvertTo-Json -Compress)
$modeJson = ($mode | ConvertTo-Json -Compress)
$auxAuditReportJson = ($AuxAuditReport | ConvertTo-Json -Compress)
$reportPathJson = ($reportPath | ConvertTo-Json -Compress)
$artifactCleanupJson = ([object[]]$artifactCleanup.ToArray() | ConvertTo-Json -Depth 20)
$selectedLessonsJson = (@($selectedLessons) | ConvertTo-Json -Depth 20)
$createdLessonsJson = ([object[]]$createdLessons.ToArray() | ConvertTo-Json -Depth 20)
$candidateCount = [int]$rankedCandidates.Count
$selectedCount = [int]$selectedLessons.Count
$createdCount = [int]$createdLessons.Count

$reportJson = @"
{
  "generated_at": $generatedAtJson,
  "mode": $modeJson,
  "aux_audit_report": $auxAuditReportJson,
  "report_path": $reportPathJson,
  "artifact_cleanup": $artifactCleanupJson,
  "lesson_seed": {
    "candidate_count": $candidateCount,
    "selected_count": $selectedCount,
    "selected": $selectedLessonsJson,
    "created_count": $createdCount,
    "created": $createdLessonsJson
  }
}
"@

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($reportPath, $reportJson, $utf8NoBom)
$reportJson
