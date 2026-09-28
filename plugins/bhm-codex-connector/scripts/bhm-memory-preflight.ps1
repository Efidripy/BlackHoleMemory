param(
    [string]$Project = "e-github-workspace",
    [string[]]$Query,
    [int]$Limit = 5,
    [switch]$AsJson
)

. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")

$pluginGuardScript = Join-Path $PSScriptRoot "bhm-plugin-duplicate-guard.ps1"
$pluginGuard = if (Test-Path -LiteralPath $pluginGuardScript) {
    & $pluginGuardScript -AsJson | ConvertFrom-Json
} else {
    [pscustomobject]@{
        ok = $false
        action = "bhm-plugin-duplicate-guard"
        action_required = $true
        recommendation = "BHM plugin duplicate guard is missing; do not install another connector copy until the plugin bundle is repaired."
    }
}

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
    # FastMCP search responses may omit either property. StrictMode turns a
    # missing optional field into a terminating error, so inspect the property
    # bag rather than accessing the member speculatively.
    $memoriesProperty = $search.PSObject.Properties['memories']
    $resultsProperty = $search.PSObject.Properties['results']
    $searchResults = if ($null -ne $memoriesProperty) {
        $memoriesProperty.Value
    } elseif ($null -ne $resultsProperty) {
        $resultsProperty.Value
    } else {
        @()
    }

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
    plugin_guard = $pluginGuard
    operator_alerts = @(if ($pluginGuard.action_required) { $pluginGuard.recommendation })
    required_closeout = "Run bhm-memory-checkpoint.ps1 before ending non-trivial work if you learned, changed, fixed, or deferred anything durable."
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 30
    exit 0
}

Write-Host ($result | ConvertTo-Json -Depth 30)
