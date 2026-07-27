param(
    [string]$Project = "e-github-workspace",
    [string[]]$Query,
    [int]$Limit = 5,
    [switch]$AsJson
)

. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")

if (-not $Query -or $Query.Count -eq 0) {
    $Query = @(
        "$Project checkpoint status known issues next",
        "$Project project conventions validation commands",
        "workspace memory protocol bhm project scope"
    )
}

$baseUrl = Resolve-ConnectorBaseUrl
$transport = New-ConnectorTransportTruth -BaseUrl $baseUrl -Operation "preflight"
$health = Invoke-ConnectorJson -Method "GET" -Path "/bhm/health" -BaseUrl $baseUrl
$diagnose = $null
try {
    $diagnose = Invoke-ConnectorJson -Method "POST" -Path "/bhm/diagnostics" -Body @{} -BaseUrl $baseUrl
} catch {
    $diagnose = [pscustomobject]@{ error = $_.Exception.Message }
}

$profile = $null
try {
    $profile = Invoke-ConnectorJson -Method "GET" -Path "/bhm/profile" -Query @{ project = $Project } -BaseUrl $baseUrl
} catch {
    $profile = [pscustomobject]@{ error = $_.Exception.Message }
}

$searches = @()
foreach ($q in $Query) {
    $search = Invoke-ConnectorJson -Method "POST" -Path "/bhm/search" -Body @{
        query = $q
        limit = $Limit
        project = $Project
    } -BaseUrl $baseUrl
    $searchResults = if ($search.memories) { $search.memories } elseif ($search.results) { $search.results } else { @() }

    $searches += [pscustomobject]@{
        query = $q
        results = $searchResults
        lessons = @()
    }
}

$result = [pscustomobject]@{
    ok = $true
    project = $Project
    baseUrl = $baseUrl
    health = [pscustomobject]@{
        status = $health.status
        service = $health.service
        version = $health.version
        viewerPort = $health.viewerPort
    }
    diagnose = $diagnose
    profile = $profile
    searches = $searches
    transport = $transport
    required_closeout = "Run bhm-memory-checkpoint.ps1 before ending non-trivial work if you learned, changed, fixed, or deferred anything durable."
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 30
    exit 0
}

Write-Host ($result | ConvertTo-Json -Depth 30)
