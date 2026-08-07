param(
    [string]$BaseUrl = '',
    [string]$Project = "blackholememory",
    [string[]]$Queries = @(
        "BHM runtime storage",
        "authoritative SQLite projection",
        "MCP attach observability",
        "context compiler retrieval",
        "Qdrant projection worker"
    ),
    [ValidateRange(1, 50)][int]$Limit = 10,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Add-Type -AssemblyName System.Net.Http
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot }

$RetrievalQualityHttpTimeoutSec = 20
$RetrievalQualityMaxResponseBytes = 262144

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

function Assert-RetrievalQualityUri {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    $parsed = $null
    if (-not [Uri]::TryCreate($Candidate, [UriKind]::Absolute, [ref]$parsed)) {
        throw 'retrieval quality URL is not an absolute URI'
    }
    $allowedHosts = @('127.0.0.1', 'localhost', '::1')
    if ($parsed.Scheme -ne 'http' -or $allowedHosts -notcontains $parsed.Host.ToLowerInvariant()) {
        throw 'retrieval quality requires an HTTP loopback endpoint'
    }
    if (-not [string]::IsNullOrWhiteSpace($parsed.UserInfo)) {
        throw 'retrieval quality URL must not contain userinfo'
    }
    if (-not [string]::IsNullOrWhiteSpace($parsed.Query)) {
        throw 'retrieval quality URL must not contain a query'
    }
    if (-not [string]::IsNullOrWhiteSpace($parsed.Fragment)) {
        throw 'retrieval quality URL must not contain a fragment'
    }
    return $parsed
}

function Invoke-RetrievalQualityJson {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [ValidateSet('GET', 'POST')][string]$Method = 'GET',
        [hashtable]$RequestHeaders = @{},
        [string]$Body = $null
    )

    $parsed = Assert-RetrievalQualityUri -Candidate $Uri
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $handler.UseProxy = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($RetrievalQualityHttpTimeoutSec)
    $httpMethod = if ($Method -eq 'POST') { [System.Net.Http.HttpMethod]::Post } else { [System.Net.Http.HttpMethod]::Get }
    $request = [System.Net.Http.HttpRequestMessage]::new($httpMethod, $parsed)
    $response = $null
    $content = $null
    try {
        foreach ($key in $RequestHeaders.Keys) {
            $request.Headers.TryAddWithoutValidation([string]$key, [string]$RequestHeaders[$key]) | Out-Null
        }
        if ($Method -eq 'POST' -and -not [string]::IsNullOrWhiteSpace($Body)) {
            $content = [System.Net.Http.StringContent]::new($Body, [Text.Encoding]::UTF8, 'application/json')
            $request.Content = $content
        }
        $response = $client.SendAsync(
            $request,
            [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw "retrieval quality returned HTTP $([int]$response.StatusCode)"
        }
        $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        if ($bytes.Length -gt $RetrievalQualityMaxResponseBytes) {
            throw "retrieval quality response exceeded bounded limit $RetrievalQualityMaxResponseBytes bytes"
        }
        return ([Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json)
    }
    finally {
        if ($null -ne $response) { $response.Dispose() }
        if ($null -ne $content) { $content.Dispose() }
        $request.Dispose()
        $client.Dispose()
        $handler.Dispose()
    }
}

function Get-LiveRuntimeState {
    param([Parameter(Mandatory = $true)][string]$Url)

    try {
        $headers = Get-BhmCallerHeaders
        $health = Invoke-RetrievalQualityJson -Uri "$($Url.TrimEnd('/'))/bhm/health" -RequestHeaders $headers
        $cutover = Invoke-RetrievalQualityJson -Uri "$($Url.TrimEnd('/'))/health/cutover" -RequestHeaders $headers
        $slo = Invoke-RetrievalQualityJson -Uri "$($Url.TrimEnd('/'))/bhm/health/slo" -RequestHeaders $headers
        return [pscustomobject]@{
            ok = (
                $health.status -eq "healthy" -and
                $health.memory_store.backend -eq "sqlite-authoritative" -and
                [bool]$health.memory_store.ready -and
                [bool]$cutover.ok -and
                $cutover.mem0.status -eq "projection-only" -and
                -not [bool]$cutover.mem0.direct_vector_writes -and
                $slo.status -eq "healthy"
            )
            health = $health.status
            version = $health.version
            memory_store = $health.memory_store.backend
            ready = [bool]$health.memory_store.ready
            cutover = [bool]$cutover.ok
            mem0_status = $cutover.mem0.status
            direct_vector_writes = [bool]$cutover.mem0.direct_vector_writes
            slo = $slo.status
            projection_pending = [int]$slo.observed.projection_pending
            projection_failed = [int]$slo.observed.projection_failed
            error = ""
        }
    } catch {
        return [pscustomobject]@{
            ok = $false
            health = "unreachable"
            version = ""
            memory_store = ""
            ready = $false
            cutover = $false
            mem0_status = ""
            direct_vector_writes = $false
            slo = "unknown"
            projection_pending = -1
            projection_failed = -1
            error = $_.Exception.Message
        }
    }
}

function Invoke-RetrievalProbe {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Query,
        [Parameter(Mandatory = $true)][string]$ProjectName,
        [Parameter(Mandatory = $true)][int]$ProbeLimit
    )

    $body = @{
        query = $Query
        project = $ProjectName
        profile = "low-context"
        limit = $ProbeLimit
    } | ConvertTo-Json -Depth 5
    try {
        $callerHeaders = Get-BhmCallerHeaders
        $compile = Invoke-RetrievalQualityJson -Uri "$($Url.TrimEnd('/'))/bhm/context/compile" -Method POST -RequestHeaders $callerHeaders -Body $body
        $explain = Invoke-RetrievalQualityJson -Uri "$($Url.TrimEnd('/'))/bhm/retrieval/explain" -Method POST -RequestHeaders $callerHeaders -Body $body
        $compileRetrieval = $compile.retrieval
        $explainRetrieval = $explain.retrieval
        $explainTotal = [int]$explain.total
        $empty = ([int]$compileRetrieval.total -eq 0 -or [int]$compileRetrieval.candidate_count -eq 0 -or $explainTotal -eq 0 -or [int]$explainRetrieval.candidate_count -eq 0)
        $ok = (
            -not $empty -and
            [bool]$compile.provenance.complete -and
            [int]$compileRetrieval.candidate_count -gt 0 -and
            [int]$compileRetrieval.eligible_count -gt 0 -and
            [int]$compileRetrieval.included_count -gt 0 -and
            [int]$explainRetrieval.included_count -gt 0
        )
        return [pscustomobject]@{
            ok = $ok
            query = $Query
            compile = [pscustomobject]@{
                total = [int]$compileRetrieval.total
                candidate_count = [int]$compileRetrieval.candidate_count
                eligible_count = [int]$compileRetrieval.eligible_count
                included_count = [int]$compileRetrieval.included_count
                provenance_complete = [bool]$compile.provenance.complete
                citation_count = [int]$compile.provenance.citation_count
            }
            explain = [pscustomobject]@{
                total = $explainTotal
                candidate_count = [int]$explainRetrieval.candidate_count
                included_count = [int]$explainRetrieval.included_count
            }
            empty_contour = $empty
            error = ""
        }
    } catch {
        return [pscustomobject]@{
            ok = $false
            query = $Query
            compile = $null
            explain = $null
            empty_contour = $true
            error = $_.Exception.Message
        }
    }
}

$runtime = Get-LiveRuntimeState -Url $BaseUrl
$probes = @()
foreach ($query in @($Queries | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
    $probes += Invoke-RetrievalProbe -Url $BaseUrl -Query $query -ProjectName $Project -ProbeLimit $Limit
}
$probeCount = $probes.Count
$emptyCount = @($probes | Where-Object { $_.empty_contour }).Count
$failedCount = @($probes | Where-Object { -not $_.ok }).Count
$emptyRate = if ($probeCount -eq 0) { 1.0 } else { [math]::Round($emptyCount / [double]$probeCount, 4) }

$result = [pscustomobject]@{
    ok = ($runtime.ok -and $probeCount -gt 0 -and $failedCount -eq 0 -and $emptyRate -eq 0.0)
    action = "read-only-live-retrieval-audit"
    mutation = $false
    base_url = $BaseUrl
    project = $Project
    endpoints = @("/bhm/context/compile", "/bhm/retrieval/explain")
    runtime = $runtime
    probes = $probes
    summary = [pscustomobject]@{
        probe_count = $probeCount
        failed_count = $failedCount
        empty_contour_count = $emptyCount
        empty_contour_rate = $emptyRate
        minimum_expected_included = 1
    }
    note = "Read-only retrieval quality gate. Empty contours are measured on the bounded query matrix; no memory, Qdrant point, or runtime state is mutated."
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 12
    if ($result.ok) { exit 0 }
    exit 1
}

Write-Host "=== BHM Live Retrieval Quality ==="
Write-Host ("Overall gate: {0}; probes: {1}; empty contour rate: {2}" -f $result.ok, $probeCount, $emptyRate)
foreach ($probe in $probes) {
    $total = if ($null -eq $probe.compile) { "error" } else { $probe.compile.total }
    $included = if ($null -eq $probe.compile) { "error" } else { $probe.compile.included_count }
    Write-Host ("{0}: total={1}, included={2}, empty={3}, ok={4}" -f $probe.query, $total, $included, $probe.empty_contour, $probe.ok)
}
if ($result.ok) { exit 0 }
exit 1
