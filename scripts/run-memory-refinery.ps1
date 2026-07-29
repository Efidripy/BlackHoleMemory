param(
    [string]$BaseUrl = '',
    [string]$Project = "",
    [string]$ExcludeProjectRegex = "^(bhm-surface-smoke-|smoke-|noise-smoke-|next20-smoke$)",
    [switch]$ApplySafe
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\runtime-endpoints.ps1')
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot (Split-Path -Parent $PSScriptRoot) }

function Get-BhmCallerToken {
    $token = [string]$env:BHM_CALLER_TOKEN
    if ($token.Length -lt 32) {
        $token = [string][Environment]::GetEnvironmentVariable('BHM_CALLER_TOKEN', 'User')
    }
    if ($token.Length -lt 32) {
        throw 'BHM caller credential is unavailable; initialize BHM_CALLER_TOKEN'
    }
    return $token
}

function Invoke-BhmJson {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Url,
        [object]$Body
    )

    $headers = @{ Authorization = 'Bearer {0}' -f (Get-BhmCallerToken) }
    if ($null -ne $Body) {
        return Invoke-RestMethod `
            -Method $Method `
            -Uri $Url `
            -Headers $headers `
            -ContentType "application/json" `
            -Body ($Body | ConvertTo-Json -Depth 12)
    }

    return Invoke-RestMethod -Method $Method -Uri $Url -Headers $headers
}

function Add-Step {
    param(
        [Parameter(Mandatory = $true)]$Steps,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    $startedAt = Get-Date
    try {
        $result = & $Action
        $finishedAt = Get-Date
        $Steps.Add([ordered]@{
            name = $Name
            ok = $true
            started_at = $startedAt.ToString("s")
            finished_at = $finishedAt.ToString("s")
            details = $result
        }) | Out-Null
    }
    catch {
        $finishedAt = Get-Date
        $responseBody = $null
        $hasResponse = $false
        if ($null -ne $_.Exception -and $_.Exception.PSObject.Properties.Name -contains "Response") {
            $hasResponse = $true
        }
        if ($hasResponse -and $null -ne $_.Exception.Response) {
            try {
                $stream = $_.Exception.Response.GetResponseStream()
                if ($stream) {
                    $reader = New-Object System.IO.StreamReader($stream)
                    $responseBody = $reader.ReadToEnd()
                    $reader.Dispose()
                }
            }
            catch {
                $responseBody = $null
            }
        }

        $Steps.Add([ordered]@{
            name = $Name
            ok = $false
            started_at = $startedAt.ToString("s")
            finished_at = $finishedAt.ToString("s")
            error = $_.Exception.Message
            response = $responseBody
        }) | Out-Null
    }
}

function Get-ProjectValue {
    if ([string]::IsNullOrWhiteSpace($Project)) {
        return $null
    }

    return $Project.Trim()
}

function Get-StringSha256 {
    param(
        [string]$Value
    )

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        $hashBytes = $sha.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hashBytes) -replace "-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Invoke-ProjectSweep {
    param(
        [string[]]$Projects,
        [Parameter(Mandatory = $true)][scriptblock]$Operation
    )

    if (@($Projects).Count -eq 0) {
        return [pscustomobject]@{
            mode = "global"
            result = & $Operation $null
        }
    }

    $results = New-Object 'System.Collections.Generic.List[object]'
    foreach ($projectName in @($Projects)) {
        $results.Add([ordered]@{
            project = $projectName
            result = & $Operation $projectName
        }) | Out-Null
    }

    return [pscustomobject]@{
        mode = "per-project"
        projects = [string[]]@($Projects)
        count = @($Projects).Count
        results = [object[]]$results.ToArray()
    }
}

function Get-SweepItems {
    param(
        [Parameter(Mandatory = $true)]$Sweep,
        [Parameter(Mandatory = $true)][string]$Property
    )

    if ($Sweep.mode -eq "global") {
        return @($Sweep.result.$Property)
    }

    $items = New-Object 'System.Collections.Generic.List[object]'
    foreach ($entry in @($Sweep.results)) {
        foreach ($item in @($entry.result.$Property)) {
            $items.Add($item) | Out-Null
        }
    }
    return @($items)
}

function Get-SweepSum {
    param(
        [Parameter(Mandatory = $true)]$Sweep,
        [Parameter(Mandatory = $true)][string]$Property
    )

    if ($Sweep.mode -eq "global") {
        $value = $Sweep.result.$Property
        if ($null -eq $value) {
            return 0
        }
        return [int]$value
    }

    $sum = 0
    foreach ($entry in @($Sweep.results)) {
        $value = $entry.result.$Property
        if ($null -ne $value) {
            $sum += [int]$value
        }
    }
    return $sum
}

$projectValue = Get-ProjectValue
$scopeLabel = if ($projectValue) { $projectValue } else { "all-projects" }
$mode = if ($ApplySafe) { "apply-safe" } else { "dry-run" }
$runId = "{0:yyyy-MM-dd_HH-mm-ss}" -f (Get-Date)
$reportDir = Join-Path $PSScriptRoot "..\.runtime\refinery"
$reportPath = Join-Path $reportDir "memory-refinery-$mode-$scopeLabel-$runId.json"
$steps = New-Object 'System.Collections.Generic.List[object]'
$refineryProjects = @()

New-Item -ItemType Directory -Force -Path $reportDir | Out-Null

Add-Step -Steps $steps -Name "service_ready" -Action {
    Invoke-BhmJson -Method Get -Url "$BaseUrl/health/ready"
}

$exportName = "memory-refinery-$mode-$scopeLabel-$runId"
Add-Step -Steps $steps -Name "admin_export" -Action {
    Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/admin/export" -Body @{
        project = $projectValue
        include_archived = $true
        include_artifacts = $true
        export_name = $exportName
    }
}

Add-Step -Steps $steps -Name "live_memory_inventory" -Action {
    $inventory = Invoke-BhmJson -Method Get -Url "$BaseUrl/bhm/memories?include_archived=true&limit=1000&offset=0"
    $projectCounts = @{}
    foreach ($item in @($inventory.memories)) {
        $projectKey = if ($item.project) { [string]$item.project } else { "<none>" }
        if (-not $projectCounts.ContainsKey($projectKey)) {
            $projectCounts[$projectKey] = 0
        }
        $projectCounts[$projectKey] += 1
    }

    $topProjects = $projectCounts.GetEnumerator() |
        Sort-Object Value -Descending |
        Select-Object -First 25 |
        ForEach-Object {
            [ordered]@{
                project = $_.Key
                count = $_.Value
            }
        }

    [ordered]@{
        memory_count = @($inventory.memories).Count
        top_projects = $topProjects
    }
}

if ($projectValue) {
    $refineryProjects = @($projectValue)
}

Add-Step -Steps $steps -Name "project_scope_inventory" -Action {
    [ordered]@{
        selected_projects = @($refineryProjects)
        excluded_regex = $ExcludeProjectRegex
        selected_project_count = @($refineryProjects).Count
    }
}

Add-Step -Steps $steps -Name "policy_profile_get" -Action {
    Invoke-BhmJson -Method Get -Url "$BaseUrl/bhm/policy/profile"
}

Add-Step -Steps $steps -Name "memory_usage_stats" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/usage-stats" -Body @{ project = $projectName }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "artifact_usage_stats" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/artifact/usage-stats" -Body @{ project = $projectName }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "project_memory_heatmap" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/project-memory-heatmap" -Body @{ project = $projectName }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "schema_validate_strict_before" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/schema/validate-strict" -Body @{
            project = $projectName
            include_archived = $true
        }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        issues = Get-SweepItems -Sweep $sweep -Property "issues"
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "integrity_audit_before" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/integrity-audit" -Body @{
            project = $projectName
        }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        duplicate_candidates = Get-SweepItems -Sweep $sweep -Property "duplicate_candidates"
        same_upsert_key = Get-SweepItems -Sweep $sweep -Property "same_upsert_key"
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "artifact_integrity_audit_before" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/artifact-integrity-audit" -Body @{
            project = $projectName
        }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        orphan_links = Get-SweepItems -Sweep $sweep -Property "orphan_links"
        orphan_artifacts = Get-SweepItems -Sweep $sweep -Property "orphan_artifacts"
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "overlap_report" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/overlap/report" -Body @{
            project = $projectName
            limit = 50
        }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        duplicate_candidates = Get-SweepItems -Sweep $sweep -Property "duplicate_candidates"
        same_upsert_key = Get-SweepItems -Sweep $sweep -Property "same_upsert_key"
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "memory_review_queue" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/review-queue" -Body @{
            project = $projectName
            limit = 50
        }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        items = Get-SweepItems -Sweep $sweep -Property "items"
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "memory_triage_queue" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/triage-queue" -Body @{
            project = $projectName
            limit = 50
        }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        items = Get-SweepItems -Sweep $sweep -Property "items"
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "memory_staleness_report" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/staleness-report" -Body @{
            project = $projectName
            days = 90
            limit = 50
        }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        items = Get-SweepItems -Sweep $sweep -Property "items"
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "memory_gc_candidates" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/gc-candidates" -Body @{
            project = $projectName
            stale_days = 90
            limit = 50
        }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        candidates = Get-SweepItems -Sweep $sweep -Property "candidates"
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "memory_compaction_report" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/compaction-report" -Body @{
            project = $projectName
            min_chars = 1200
            min_lines = 25
            limit = 50
        }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        candidates = Get-SweepItems -Sweep $sweep -Property "candidates"
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "link_cycle_detect" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/link/cycle-detect" -Body @{
            project = $projectName
            limit = 50
        }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        cycles = Get-SweepItems -Sweep $sweep -Property "cycles"
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "link_orphan_scan" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/link/orphan-scan" -Body @{
            project = $projectName
        }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        orphan_links = Get-SweepItems -Sweep $sweep -Property "orphan_links"
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

Add-Step -Steps $steps -Name "recent_failures_feed" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/recent-failures-feed" -Body @{
            project = $projectName
            limit = 50
        }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        failures = Get-SweepItems -Sweep $sweep -Property "items"
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

if ($ApplySafe) {
    Add-Step -Steps $steps -Name "memory_normalize_metadata" -Action {
        Invoke-ProjectSweep -Projects $refineryProjects -Operation {
            param($projectName)
            Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/normalize-metadata" -Body @{ project = $projectName }
        }
    }

    Add-Step -Steps $steps -Name "reindex_memory_metadata" -Action {
        Invoke-ProjectSweep -Projects $refineryProjects -Operation {
            param($projectName)
            Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/reindex-memory-metadata" -Body @{ project = $projectName }
        }
    }

    Add-Step -Steps $steps -Name "schema_upgrade_all" -Action {
        Invoke-ProjectSweep -Projects $refineryProjects -Operation {
            param($projectName)
            Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/schema/upgrade-all" -Body @{ project = $projectName }
        }
    }

    Add-Step -Steps $steps -Name "repair_live_indexes" -Action {
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/repair-live-indexes" -Body @{
            remove_orphan_links = $true
            remove_orphan_artifacts = $true
        }
    }

    Add-Step -Steps $steps -Name "secret_scan_existing_memories" -Action {
        $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
            param($projectName)
            Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/secret-scan" -Body @{
                project = $projectName
                limit = 100
            }
        }
        [ordered]@{
            mode = $sweep.mode
            projects = @($refineryProjects)
            findings = Get-SweepItems -Sweep $sweep -Property "findings"
            results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
        }
    }
}

Add-Step -Steps $steps -Name "entity_catalog_rebuild" -Action {
    Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/entity-catalog/rebuild" -Body @{ project = $projectName }
    }
}

Add-Step -Steps $steps -Name "entity_catalog_get" -Action {
    Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/entity-catalog/get" -Body @{ project = $projectName }
    }
}

Add-Step -Steps $steps -Name "relation_suggest" -Action {
    $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
        param($projectName)
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/relation-suggest" -Body @{
            project = $projectName
            limit = 50
        }
    }
    [ordered]@{
        mode = $sweep.mode
        projects = @($refineryProjects)
        suggestions = Get-SweepItems -Sweep $sweep -Property "suggestions"
        results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
    }
}

if ($ApplySafe) {
    Add-Step -Steps $steps -Name "relation_apply_suggestions" -Action {
        Invoke-ProjectSweep -Projects $refineryProjects -Operation {
            param($projectName)
            Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/relation/apply-suggestions" -Body @{
                project = $projectName
                min_score = 0.9
                limit = 50
                include_relates_to = $false
            }
        }
    }

    Add-Step -Steps $steps -Name "review_queue_apply" -Action {
        Invoke-ProjectSweep -Projects $refineryProjects -Operation {
            param($projectName)
            Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/review-queue/apply" -Body @{
                project = $projectName
                limit = 50
                mark_needs_review = $true
                auto_redact_secrets = $true
            }
        }
    }
}

Add-Step -Steps $steps -Name "project_summary_refresh_all" -Action {
    $projects = if (@($refineryProjects).Count -gt 0) { @($refineryProjects) } else { $null }
    Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/project-summary/refresh-all" -Body @{
        projects = $projects
    }
}

if ($ApplySafe) {
    Add-Step -Steps $steps -Name "schema_validate_strict_after" -Action {
        $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
            param($projectName)
            Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/schema/validate-strict" -Body @{
                project = $projectName
                include_archived = $true
            }
        }
        [ordered]@{
            mode = $sweep.mode
            projects = @($refineryProjects)
            issues = Get-SweepItems -Sweep $sweep -Property "issues"
            results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
        }
    }

    Add-Step -Steps $steps -Name "integrity_audit_after" -Action {
        $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
            param($projectName)
            Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/integrity-audit" -Body @{
                project = $projectName
            }
        }
        [ordered]@{
            mode = $sweep.mode
            projects = @($refineryProjects)
            duplicate_candidates = Get-SweepItems -Sweep $sweep -Property "duplicate_candidates"
            same_upsert_key = Get-SweepItems -Sweep $sweep -Property "same_upsert_key"
            results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
        }
    }

    Add-Step -Steps $steps -Name "artifact_integrity_audit_after" -Action {
        $sweep = Invoke-ProjectSweep -Projects $refineryProjects -Operation {
            param($projectName)
            Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/artifact-integrity-audit" -Body @{
                project = $projectName
            }
        }
        [ordered]@{
            mode = $sweep.mode
            projects = @($refineryProjects)
            orphan_links = Get-SweepItems -Sweep $sweep -Property "orphan_links"
            orphan_artifacts = Get-SweepItems -Sweep $sweep -Property "orphan_artifacts"
            results = if ($sweep.mode -eq "global") { @($sweep.result) } else { @($sweep.results) }
        }
    }
}

$failed = @($steps | Where-Object { -not $_.ok })
$summary = [ordered]@{
    generated_at = (Get-Date).ToString("s")
    mode = $mode
    project = $projectValue
    scope_label = $scopeLabel
    base_url = $BaseUrl
    report_path = $reportPath
    steps_total = $steps.Count
    steps_failed = $failed.Count
    steps = $steps
}

$summary | ConvertTo-Json -Depth 14 | Set-Content -Encoding UTF8 $reportPath
$summary | ConvertTo-Json -Depth 14

if ($failed.Count -gt 0) {
    exit 1
}
