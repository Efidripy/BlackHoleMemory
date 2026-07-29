param(
    [string]$BaseUrl = '',
    [string]$QdrantBaseUrl = '',
    [switch]$IncludeAdmin
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot }
if ([string]::IsNullOrWhiteSpace($QdrantBaseUrl)) { $QdrantBaseUrl = Get-BhmRuntimeEndpoint -Name 'qdrant_http' -RepoRoot $repoRoot }
$script:BhmAdminHeaders = @{}
$script:BhmAdminCapability = @(
    $env:BHM_ADMIN_CAPABILITY,
    $env:BHM_MCP_ADMIN_CAPABILITY
) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -First 1
$script:BhmCapabilityConfigured = -not [string]::IsNullOrWhiteSpace($script:BhmAdminCapability)
if ($script:BhmCapabilityConfigured) {
    $script:BhmAdminHeaders["X-BHM-Admin-Capability"] = $script:BhmAdminCapability
}

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

function Test-BhmCallerAuthRequired {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Url
    )

    if ($Method.ToUpperInvariant() -eq 'OPTIONS') { return $false }
    $path = ([Uri]$Url).AbsolutePath
    if ($path -in @('/bhm/health', '/bhm/health/slo')) { return $false }
    return (
        $path -eq '/mcp' -or
        $path -eq '/openapi-admin.json' -or
        $path -eq '/graph/status' -or
        $path -eq '/bhm' -or
        $path.StartsWith('/bhm/', [StringComparison]::Ordinal)
    )
}

function Get-BhmRequestHeaders {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Url
    )

    $headers = @{}
    if (-not (Test-BhmCallerAuthRequired -Method $Method -Url $Url)) {
        return $headers
    }
    foreach ($entry in (Get-BhmCallerHeaders).GetEnumerator()) {
        $headers[$entry.Key] = $entry.Value
    }
    foreach ($entry in $script:BhmAdminHeaders.GetEnumerator()) {
        $headers[$entry.Key] = $entry.Value
    }
    return $headers
}

function Invoke-BhmJsonRequest {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Url,
        [object]$Body
    )

    $headers = Get-BhmRequestHeaders -Method $Method -Url $Url
    if ($null -ne $Body) {
        return Invoke-RestMethod -Method $Method -Uri $Url -Headers $headers -ContentType "application/json" -Body ($Body | ConvertTo-Json -Depth 10)
    }

    return Invoke-RestMethod -Method $Method -Uri $Url -Headers $headers
}

function Invoke-BhmJson {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Url,
        [object]$Body,
        [int]$Attempts = 3,
        [int]$DelayMilliseconds = 350
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            return Invoke-BhmJsonRequest -Method $Method -Url $Url -Body $Body
        }
        catch {
            $statusCode = $null
            if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
                $statusCode = [int]$_.Exception.Response.StatusCode
            }

            $isRetryable = $null -eq $statusCode -or $statusCode -ge 500
            if ($attempt -ge $Attempts -or -not $isRetryable) {
                throw
            }

            Start-Sleep -Milliseconds ($DelayMilliseconds * $attempt)
        }
    }
}

function Add-CheckResult {
    param(
        [Parameter(Mandatory = $true)]$Checks,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    try {
        $result = & $Action
        $Checks.Add([ordered]@{
            name = $Name
            ok = $true
            details = $result
        }) | Out-Null
    }
    catch {
        $responseBody = $null
        if ($_.Exception.Response) {
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

        $Checks.Add([ordered]@{
            name = $Name
            ok = $false
            error = $_.Exception.Message
            response = $responseBody
        }) | Out-Null
    }
}

function New-TaxonomyMetadata {
    param(
        [string]$Priority = "normal",
        [string]$SemanticType = "log"
    )

    return @{
        lifecycle = "validated"
        provenance = "synthetic"
        priority = $Priority
        domain = "infra"
        sensitivity = "internal"
        scope = "service"
        retention = "short-term"
        verification = "trusted"
        actionability = "task"
        stakeholder = "devops"
        language = "code-python"
        semantic_type = $SemanticType
        version = "1.0"
        origin = "surface-smoke"
        surface = "stable"
    }
}

function Get-BhmLocalCollectionName {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectName
    )

    $slug = (($ProjectName.Trim().ToLowerInvariant() -replace "[^a-z0-9]+", "_").Trim("_"))
    if ([string]::IsNullOrWhiteSpace($slug)) {
        throw "Cannot build Qdrant collection name from empty project."
    }

    return "bhm_local_memory_$slug"
}

function Remove-QdrantCollectionBestEffort {
    param(
        [Parameter(Mandatory = $true)][string]$CollectionName,
        [Parameter(Mandatory = $true)][string]$QdrantBaseUrl
    )

    try {
        $encodedName = [System.Uri]::EscapeDataString($CollectionName)
        Invoke-RestMethod `
            -Method Delete `
            -Uri "$($QdrantBaseUrl.TrimEnd('/'))/collections/$encodedName" `
            -ErrorAction Stop | Out-Null
        Write-Verbose "Cleaned smoke Qdrant collection: $CollectionName"
    }
    catch {
        Write-Verbose "Smoke Qdrant cleanup failed for ${CollectionName}: $($_.Exception.Message)"
    }
}

function Remove-SurfaceSmokeProject {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectName,
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$QdrantBaseUrl,
        [Parameter(Mandatory = $true)][string]$LocalCollectionName
    )

    $encodedProject = [System.Uri]::EscapeDataString($ProjectName)
    $memoryResponse = Invoke-BhmJson `
        -Method Get `
        -Url "$($BaseUrl.TrimEnd('/'))/bhm/memories?project=$encodedProject&limit=200&offset=0"
    $memories = @($memoryResponse.memories)
    $globalPointIds = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($memory in $memories) {
        foreach ($pointId in @($memory.metadata.global_mem0_ids)) {
            if (-not [string]::IsNullOrWhiteSpace([string]$pointId)) {
                $globalPointIds.Add([string]$pointId) | Out-Null
            }
        }
    }

    $globalIds = @($globalPointIds | Sort-Object)
    if ($globalIds.Count -gt 0) {
        Invoke-RestMethod `
            -Method Post `
            -Uri "$($QdrantBaseUrl.TrimEnd('/'))/collections/bhm_global_core_knowledge/points/delete?wait=true" `
            -ContentType "application/json" `
            -Body (@{ points = $globalIds } | ConvertTo-Json -Depth 5) `
            -ErrorAction Stop | Out-Null
    }

    foreach ($memory in $memories) {
        Invoke-BhmJson `
            -Method Delete `
            -Url "$($BaseUrl.TrimEnd('/'))/bhm/memory/hard" `
            -Body @{
                id = [string]$memory.id
                project = $ProjectName
            } | Out-Null
    }
    Remove-QdrantCollectionBestEffort `
        -CollectionName $LocalCollectionName `
        -QdrantBaseUrl $QdrantBaseUrl

    $remaining = Invoke-BhmJson `
        -Method Get `
        -Url "$($BaseUrl.TrimEnd('/'))/bhm/memories?project=$encodedProject&limit=10&offset=0"
    if ([int]$remaining.total -ne 0) {
        throw "Surface smoke cleanup left $($remaining.total) live memories for $ProjectName."
    }

    $remainingGlobalPoints = 0
    if ($globalIds.Count -gt 0) {
        $retrieved = Invoke-RestMethod `
            -Method Post `
            -Uri "$($QdrantBaseUrl.TrimEnd('/'))/collections/bhm_global_core_knowledge/points" `
            -ContentType "application/json" `
            -Body (@{ ids = $globalIds; with_payload = $false; with_vector = $false } | ConvertTo-Json -Depth 5) `
            -ErrorAction Stop
        $remainingGlobalPoints = @($retrieved.result).Count
    }
    if ($remainingGlobalPoints -ne 0) {
        throw "Surface smoke cleanup left $remainingGlobalPoints global Qdrant points for $ProjectName."
    }

    return [ordered]@{
        project = $ProjectName
        deleted_memories = $memories.Count
        deleted_global_points = $globalIds.Count
        remaining_memories = [int]$remaining.total
        remaining_global_points = $remainingGlobalPoints
        local_collection = $LocalCollectionName
    }
}

$runId = [guid]::NewGuid().ToString("N").Substring(0, 8)
$project = "bhm-surface-smoke-$runId"
$smokeCollectionName = Get-BhmLocalCollectionName -ProjectName $project
$checks = New-Object 'System.Collections.Generic.List[object]'
$memoryAKey = "surface-smoke:${project}:memory-a"
$memoryBKey = "surface-smoke:${project}:memory-b"
$batchMemoryAKey = "surface-smoke:${project}:batch-memory-a"
$batchMemoryBKey = "surface-smoke:${project}:batch-memory-b"
$checkpointKey = "surface-smoke:${project}:checkpoint"
$projectMapKey = "surface-smoke:${project}:project-map"
$validationKey = "surface-smoke:${project}:validation"
$sessionRecordKey = "surface-smoke:${project}:session-record"
$scriptExitCode = 0

if ($IncludeAdmin -and -not $script:BhmCapabilityConfigured) {
    throw "-IncludeAdmin requires BHM_ADMIN_CAPABILITY to be configured for the BHM service."
}

if (-not $script:BhmCapabilityConfigured -and -not $IncludeAdmin) {
    Add-CheckResult -Checks $checks -Name "health_ready" -Action {
        Invoke-BhmJson -Method Get -Url "$BaseUrl/health/ready"
    }

    Add-CheckResult -Checks $checks -Name "bhm_health" -Action {
        Invoke-BhmJson -Method Get -Url "$BaseUrl/bhm/health"
    }

    Add-CheckResult -Checks $checks -Name "health_cutover" -Action {
        Invoke-BhmJson -Method Get -Url "$BaseUrl/health/cutover"
    }

    Add-CheckResult -Checks $checks -Name "public_memory_list" -Action {
        Invoke-BhmJson -Method Get -Url "$BaseUrl/bhm/memories?project=blackholememory&limit=1&offset=0"
    }

    Add-CheckResult -Checks $checks -Name "public_search_advanced" -Action {
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/search/advanced" -Body @{
            query = "BHM runtime storage"
            project = "blackholememory"
            limit = 1
            include_logs = $false
        }
    }

    Add-CheckResult -Checks $checks -Name "admin_capability_guard" -Action {
        try {
            Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/link" -Body @{
                source_id = "mem_bhm_surface_read_only_probe"
                target_id = "mem_bhm_surface_read_only_probe"
                relation = "relates_to"
                project = "blackholememory"
            } | Out-Null
            throw "protected memory link route unexpectedly allowed without capability"
        }
        catch {
            $statusCode = $null
            if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
                $statusCode = [int]$_.Exception.Response.StatusCode
            }
            if ($statusCode -ne 403) {
                throw
            }
            return [ordered]@{
                expected_denied = $true
                status = $statusCode
            }
        }
    }

    $failed = @($checks | Where-Object { -not $_.ok })
    $summary = [ordered]@{
        ok = ($failed.Count -eq 0)
        mode = "stable-read-only"
        base_url = $BaseUrl
        project = $null
        checks_total = $checks.Count
        checks_failed = $failed.Count
        checks = $checks
    }
    $summary | ConvertTo-Json -Depth 10
    if ($failed.Count -gt 0) {
        exit 1
    }
    exit 0
}

try {

$memoryA = Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/upsert" -Body @{
    upsert_key = $memoryAKey
    project = $project
    type = "workflow"
    content = "Primary smoke memory A for consolidated MCP surface validation."
    concepts = @("surface-smoke", "validation")
    files = @("scripts/validate-bhm-mcp-surface.ps1")
    metadata = New-TaxonomyMetadata -Priority "normal" -SemanticType "log"
}

$memoryB = Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/upsert" -Body @{
    upsert_key = $memoryBKey
    project = $project
    type = "workflow"
    content = "Primary smoke memory B for consolidated MCP surface validation."
    concepts = @("surface-smoke", "validation")
    files = @(".docs/ops/bhm-mcp-surface-governance.md")
    metadata = New-TaxonomyMetadata -Priority "normal" -SemanticType "log"
}

$memoryAId = $memoryA.memory.id
$memoryBId = $memoryB.memory.id

Add-CheckResult -Checks $checks -Name "health_ready" -Action {
    Invoke-BhmJson -Method Get -Url "$BaseUrl/health/ready"
}

Add-CheckResult -Checks $checks -Name "bhm_health" -Action {
    Invoke-BhmJson -Method Get -Url "$BaseUrl/bhm/health"
}

Add-CheckResult -Checks $checks -Name "search_advanced" -Action {
    Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/search/advanced" -Body @{
        project = $project
        query = "surface-smoke"
        limit = 5
        offset = 0
    }
}

Add-CheckResult -Checks $checks -Name "list_memories" -Action {
    Invoke-BhmJson -Method Get -Url "$BaseUrl/bhm/memories?project=$project&limit=10&offset=0"
}

Add-CheckResult -Checks $checks -Name "memory_metadata_update" -Action {
    $result = Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/update" -Body @{
        id = $memoryAId
        project = $project
        metadata_patch = @{
            reviewed_by = "validate-bhm-mcp-surface.ps1"
            metadata_patch_smoke = $true
            verification = "peer-reviewed"
            actionability = "decision"
        }
    }
    if ($result.memory.metadata.reviewed_by -ne "validate-bhm-mcp-surface.ps1" -or -not $result.memory.metadata.metadata_patch_smoke) {
        throw "metadata_patch was not persisted; active BHM service is likely running an older app.py"
    }
    if ($result.memory.metadata.verification -ne "peer-reviewed" -or $result.memory.metadata.actionability -ne "decision") {
        throw "typed taxonomy metadata_patch was not persisted"
    }
    $result
}

Add-CheckResult -Checks $checks -Name "batch_upsert_memories" -Action {
    $batchMetadataA = New-TaxonomyMetadata -Priority "high" -SemanticType "log"
    $batchMetadataA["batch_index"] = 1
    $batchMetadataB = New-TaxonomyMetadata -Priority "normal" -SemanticType "log"
    $batchMetadataB["batch_index"] = 2

    $result = Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memories/batch-upsert" -Body @{
        items = @(
            @{
                upsert_key = $batchMemoryAKey
                project = $project
                type = "workflow"
                content = "Batch smoke memory A for typed MCP batch validation."
                concepts = @("surface-smoke", "batch")
                files = @("src/blackholememory/bhm_mcp.py")
                metadata = $batchMetadataA
            },
            @{
                upsert_key = $batchMemoryBKey
                project = $project
                type = "workflow"
                content = "Batch smoke memory B for typed MCP batch validation."
                concepts = @("surface-smoke", "batch")
                files = @("src/blackholememory/app.py")
                metadata = $batchMetadataB
            }
        )
    }
    if ($null -eq $result.upserted_ids) {
        throw "batch_upsert_memories did not return upserted_ids; active BHM service is likely running an older app.py"
    }
    if (-not $result.upserted_ids.PSObject.Properties[$batchMemoryAKey].Value -or -not $result.upserted_ids.PSObject.Properties[$batchMemoryBKey].Value) {
        throw "batch_upsert_memories returned upserted_ids without expected batch keys"
    }
    foreach ($resultItem in @($result.items)) {
        $memory = $resultItem.memory
        if (-not $memory) {
            throw "batch_upsert_memories returned an item without memory details"
        }
        if ($memory.metadata.origin -ne "surface-smoke" -or $null -eq $memory.metadata.batch_index) {
            throw "batch_upsert_memories did not persist item metadata"
        }
        if (
            $memory.metadata.lifecycle -ne "validated" -or
            $memory.metadata.provenance -ne "synthetic" -or
            $memory.metadata.priority -notin @("high", "normal") -or
            $memory.metadata.domain -ne "infra" -or
            $memory.metadata.sensitivity -ne "internal" -or
            $memory.metadata.scope -ne "service" -or
            $memory.metadata.retention -ne "short-term" -or
            $memory.metadata.verification -ne "trusted" -or
            $memory.metadata.actionability -ne "task" -or
            $memory.metadata.stakeholder -ne "devops" -or
            $memory.metadata.language -ne "code-python" -or
            $memory.metadata.semantic_type -ne "log" -or
            $memory.metadata.version -ne "1.0"
        ) {
            throw "batch_upsert_memories did not persist typed 13-dimension taxonomy metadata"
        }
    }
    $result
}

Add-CheckResult -Checks $checks -Name "taxonomy_metadata_rejects_invalid_enum" -Action {
    try {
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/upsert" -Body @{
            upsert_key = "surface-smoke:${project}:invalid-taxonomy"
            project = $project
            type = "workflow"
            content = "Invalid taxonomy smoke memory; should not be persisted."
            concepts = @("surface-smoke", "taxonomy")
            metadata = @{
                priority = "urgent"
            }
        } | Out-Null
    }
    catch {
        return @{
            rejected = $true
            error = $_.Exception.Message
        }
    }

    throw "invalid taxonomy metadata enum was accepted"
}

Add-CheckResult -Checks $checks -Name "batch_link_memories" -Action {
    $batchResult = $checks | Where-Object { $_.name -eq "batch_upsert_memories" } | Select-Object -First 1
    if (-not $batchResult -or -not $batchResult.ok) {
        throw "batch_upsert_memories failed; batch link cannot run"
    }

    $upsertedIds = $batchResult.details.upserted_ids
    if ($null -eq $upsertedIds) {
        throw "batch_upsert_memories did not return upserted_ids; batch link cannot run"
    }
    $batchMemoryAId = $upsertedIds.PSObject.Properties[$batchMemoryAKey].Value
    $batchMemoryBId = $upsertedIds.PSObject.Properties[$batchMemoryBKey].Value
    if (-not $batchMemoryAId -or -not $batchMemoryBId) {
        throw "batch_upsert_memories did not return expected batch memory ids"
    }

    Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memories/batch-link" -Body @{
        items = @(
            @{
                source_id = $batchMemoryAId
                target_id = $batchMemoryBId
                relation = "relates_to"
                project = $project
                metadata = New-TaxonomyMetadata -Priority "normal" -SemanticType "log"
            }
        )
    }
}

Add-CheckResult -Checks $checks -Name "checkpoint_create" -Action {
    Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/checkpoint" -Body @{
        project = $project
        checkpoint_type = "workflow"
        title = "surface smoke checkpoint"
        done = "created validation checkpoint"
        next = "validate public MCP surface"
        checks = "stable smoke"
        risks = "none"
        upsert_key = $checkpointKey
    }
}

Add-CheckResult -Checks $checks -Name "checkpoint_latest" -Action {
    Invoke-BhmJson -Method Get -Url "$BaseUrl/bhm/checkpoint/latest?project=$project"
}

Add-CheckResult -Checks $checks -Name "session_record_create" -Action {
    Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/session-record" -Body @{
        project = $project
        title = "surface smoke session"
        done = "session record created"
        next = "validate session record flow"
        checks = "stable smoke"
        risks = "none"
        decisions = "surface validation only"
        conversation_notes = "generated by validate-bhm-mcp-surface.ps1"
        files_touched = @("scripts/validate-bhm-mcp-surface.ps1")
        upsert_key = $sessionRecordKey
    }
}

Add-CheckResult -Checks $checks -Name "session_record_list" -Action {
    Invoke-BhmJson -Method Get -Url "$BaseUrl/bhm/session-records?project=$project&limit=10&offset=0"
}

Add-CheckResult -Checks $checks -Name "project_map_upsert" -Action {
    Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/project-map" -Body @{
        project = $project
        title = "surface smoke map"
        auth = "none"
        routing = "validation path"
        tests = "consolidated smoke"
        deploy = "n/a"
        risks = "none"
        notes = "generated by validate-bhm-mcp-surface.ps1"
        upsert_key = $projectMapKey
    }
}

Add-CheckResult -Checks $checks -Name "project_map_get" -Action {
    Invoke-BhmJson -Method Get -Url "$BaseUrl/bhm/project-map?project=$project"
}

Add-CheckResult -Checks $checks -Name "project_summary_rebuild" -Action {
    Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/project-summary/rebuild" -Body @{
        project = $project
    }
}

Add-CheckResult -Checks $checks -Name "project_summary_get" -Action {
    Invoke-BhmJson -Method Get -Url "$BaseUrl/bhm/project-summary?project=$project"
}

Add-CheckResult -Checks $checks -Name "validation_snapshot_save" -Action {
    Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/validation-snapshot" -Body @{
        project = $project
        title = "surface smoke validation"
        lint = "ok"
        tests = "ok"
        smoke = "ok"
        docs = "ok"
        overall_status = "green"
        command_summary = "validate-bhm-mcp-surface.ps1"
        upsert_key = $validationKey
    }
}

Add-CheckResult -Checks $checks -Name "validation_snapshot_get" -Action {
    Invoke-BhmJson -Method Get -Url "$BaseUrl/bhm/validation-snapshot?project=$project"
}

Add-CheckResult -Checks $checks -Name "agent_activity_rollup" -Action {
    Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/agent-activity-rollup" -Body @{
        project = $project
    }
}

Add-CheckResult -Checks $checks -Name "memory_link_create" -Action {
    Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/memory/link" -Body @{
        source_id = $memoryAId
        target_id = $memoryBId
        relation = "relates_to"
        project = $project
        metadata = @{ origin = "surface-smoke" }
    }
}

Add-CheckResult -Checks $checks -Name "memory_links_get" -Action {
    Invoke-BhmJson -Method Get -Url "$BaseUrl/bhm/memory/links?id=$memoryAId&project=$project"
}

if ($IncludeAdmin) {
    Add-CheckResult -Checks $checks -Name "schema_validate_strict" -Action {
        $result = Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/schema/validate-strict" -Body @{
            project = $project
            include_archived = $true
        }
        [ordered]@{
            request_ok = $true
            schema_ok = $result.ok
            memory_issue_count = @($result.memory_issues).Count
            artifact_orphan_keys = @($result.artifact_orphans.PSObject.Properties.Name)
        }
    }

    $exportName = "surface-smoke-export-$runId"

    Add-CheckResult -Checks $checks -Name "admin_export" -Action {
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/admin/export" -Body @{
            project = $project
            include_archived = $true
            include_artifacts = $true
            export_name = $exportName
        }
    }

    Add-CheckResult -Checks $checks -Name "admin_import_preview" -Action {
        $exportResult = $checks | Where-Object { $_.name -eq "admin_export" } | Select-Object -First 1
        if (-not $exportResult -or -not $exportResult.ok) {
            throw "admin_export failed; preview cannot run"
        }

        $exportPath = $exportResult.details.path
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/admin/import-preview" -Body @{
            path = $exportPath
        }
    }

    Add-CheckResult -Checks $checks -Name "policy_profile_get" -Action {
        Invoke-BhmJson -Method Get -Url "$BaseUrl/bhm/policy/profile"
    }

    Add-CheckResult -Checks $checks -Name "overlap_report" -Action {
        Invoke-BhmJson -Method Post -Url "$BaseUrl/bhm/overlap/report" -Body @{
            project = $project
            limit = 10
        }
    }
}

Add-CheckResult -Checks $checks -Name "surface_smoke_cleanup" -Action {
    Remove-SurfaceSmokeProject `
        -ProjectName $project `
        -BaseUrl $BaseUrl `
        -QdrantBaseUrl $QdrantBaseUrl `
        -LocalCollectionName $smokeCollectionName
}

$failed = @($checks | Where-Object { -not $_.ok })
$summary = [ordered]@{
    ok = ($failed.Count -eq 0)
    mode = if ($IncludeAdmin) { "stable+admin" } else { "stable" }
    base_url = $BaseUrl
    project = $project
    checks_total = $checks.Count
    checks_failed = $failed.Count
    checks = $checks
}

$summary | ConvertTo-Json -Depth 10

if ($failed.Count -gt 0) {
    $scriptExitCode = 1
}
}
finally {
    Remove-QdrantCollectionBestEffort -CollectionName $smokeCollectionName -QdrantBaseUrl $QdrantBaseUrl
}

if ($scriptExitCode -ne 0) {
    exit $scriptExitCode
}
