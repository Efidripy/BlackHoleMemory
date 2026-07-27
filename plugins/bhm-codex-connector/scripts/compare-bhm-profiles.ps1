param(
    [string]$Project = "blackholememory",
    [string]$ArtifactDir = "",
    [ValidateRange(1, 20)]
    [int]$ContextRuns = 3,
    [ValidateRange(100, 60000)]
    [int]$ContextMaxLatencyMs = 5000,
    [switch]$Lightweight = $true,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ArtifactDir)) {
    $ArtifactDir = Join-Path ([System.IO.Path]::GetTempPath()) "bhm-plugin-smoke\profile-compare"
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$testScript = Join-Path $scriptRoot "test-bhm-profile.ps1"
New-Item -ItemType Directory -Force -Path $ArtifactDir | Out-Null

function Invoke-ProfileTestJson {
    param(
        [string]$ProfileName,
        [string]$RunTitle
    )

    $args = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $testScript,
        "-Profile", $ProfileName,
        "-Project", $Project,
        "-Title", $RunTitle,
        "-ContextRuns", [string]$ContextRuns,
        "-ContextMaxLatencyMs", [string]$ContextMaxLatencyMs,
        "-AsJson"
    )

    if ($Lightweight) {
        $args += "-Lightweight"
    }

    return (& powershell @args)
}

$standardJson = Invoke-ProfileTestJson -ProfileName standard -RunTitle "bhm-standard-verification"
$standard = $standardJson | ConvertFrom-Json

$deepJson = Invoke-ProfileTestJson -ProfileName deep -RunTitle "bhm-deep-verification"
$deep = $deepJson | ConvertFrom-Json

$lowJson = Invoke-ProfileTestJson -ProfileName low-context -RunTitle "bhm-low-context-verification"
$low = $lowJson | ConvertFrom-Json

function Get-Score {
    param([object]$Result)
    $metrics = $Result.metrics
    $runtime = $Result.runtime_metrics
    return (
        [int]$metrics.context_size_exceeded +
        [int]$metrics.circuit_breaker_open +
        [int]$metrics.summarize_failed +
        [int]$metrics.compression_failed +
        [int]$metrics.graph_extraction_failed +
        [int]$Result.worker_error_score +
        [int]$runtime.failed_runs +
        [int]$runtime.latency_threshold_exceeded +
        [int]$runtime.profile_contract_mismatches +
        [int]$runtime.token_budget_violations +
        [int]$runtime.retrieval_limit_violations +
        [int]$runtime.omission_contract_violations
    )
}

$standardScore = Get-Score -Result $standard
$deepScore = Get-Score -Result $deep
$lowScore = Get-Score -Result $low

$ranked = @(
    [pscustomobject]@{ name = "low-context"; score = $lowScore; p95 = $low.runtime_metrics.latency_ms.p95 },
    [pscustomobject]@{ name = "standard"; score = $standardScore; p95 = $standard.runtime_metrics.latency_ms.p95 },
    [pscustomobject]@{ name = "deep"; score = $deepScore; p95 = $deep.runtime_metrics.latency_ms.p95 }
) | Sort-Object -Property @{ Expression = "score"; Ascending = $true }, @{ Expression = "p95"; Ascending = $true }, @{ Expression = "name"; Ascending = $true }
$recommended = [string]$ranked[0].name

$result = [ordered]@{
    ok = @(@($standard, $deep, $low) | Where-Object { -not $_.ok }).Count -eq 0
    project = $Project
    generated_at = (Get-Date).ToString("o")
    recommended_profile = $recommended
    standard = $standard
    deep = $deep
    low_context = $low
    scores = [ordered]@{
        standard = $standardScore
        deep = $deepScore
        low_context = $lowScore
    }
    runtime_p95_ms = [ordered]@{
        standard = $standard.runtime_metrics.latency_ms.p95
        deep = $deep.runtime_metrics.latency_ms.p95
        low_context = $low.runtime_metrics.latency_ms.p95
    }
    context_runs = $ContextRuns
    context_max_latency_ms = $ContextMaxLatencyMs
    lightweight = [bool]$Lightweight
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$resultPath = Join-Path $ArtifactDir ("compare-{0}.json" -f $stamp)
$latestPath = Join-Path $ArtifactDir "latest.json"
$json = $result | ConvertTo-Json -Depth 30
[System.IO.File]::WriteAllText($resultPath, $json, [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText($latestPath, $json, [System.Text.UTF8Encoding]::new($false))

if ($AsJson) {
    $result | ConvertTo-Json -Depth 30
    exit 0
}

Write-Host "=== BHM Profile Compare ==="
Write-Host "- recommended : $recommended"
Write-Host "- standard    : $standardScore"
Write-Host "- low-context : $lowScore"
Write-Host "- latest      : $latestPath"
