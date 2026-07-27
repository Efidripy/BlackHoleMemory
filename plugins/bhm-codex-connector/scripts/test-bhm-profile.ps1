param(
    [ValidateSet("standard", "low-context", "deep")]
    [string]$Profile = "low-context",
    [string]$Project = "e-github-workspace",
    [string]$Title = "bhm-profile-test",
    [string]$WorkerLogPath = "C:\Users\xman\.codex\plugin-data\bhm\runtime\logs\bhm-worker-stderr.log",
    [string]$ArtifactDir = "C:\Users\xman\.codex\plugin-data\bhm\runtime\logs\profile-tests",
    [ValidateRange(1, 20)]
    [int]$ContextRuns = 3,
    [ValidateRange(100, 60000)]
    [int]$ContextMaxLatencyMs = 5000,
    [string]$ContextQuery = "profile runtime validation",
    [switch]$Lightweight,
    [switch]$Strict,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$setProfileScript = Join-Path $scriptRoot "set-bhm-profile.ps1"
$bundleScript = Join-Path $scriptRoot "generate-plugin-smoke-bundle.ps1"
$preflightScript = Join-Path $scriptRoot "bhm-memory-preflight.ps1"
$showMcpScript = Join-Path $scriptRoot "bhm-show-mcp-sources.ps1"
. (Join-Path $scriptRoot "bhm-memory-common.ps1")

New-Item -ItemType Directory -Force -Path $ArtifactDir | Out-Null

function Write-Utf8Json {
    param(
        [string]$Path,
        [object]$Value
    )

    $json = $Value | ConvertTo-Json -Depth 30
    [System.IO.File]::WriteAllText($Path, $json, [System.Text.UTF8Encoding]::new($false))
}

function Get-Percentile {
    param(
        [double[]]$Values,
        [double]$Percentile
    )

    if (-not $Values -or $Values.Count -eq 0) {
        return $null
    }
    $sorted = @($Values | Sort-Object)
    $index = [Math]::Min($sorted.Count - 1, [Math]::Max(0, [int][Math]::Ceiling($sorted.Count * $Percentile) - 1))
    return [double]$sorted[$index]
}

function Invoke-ContextProfileRuntimeMetrics {
    param(
        [string]$ProfileName,
        [string]$ProjectName,
        [string]$Query,
        [int]$Runs,
        [int]$MaxLatencyMs
    )

    $baseUrl = Resolve-ConnectorBaseUrl
    $samples = @()
    $errors = @()
    $profileContractMismatches = 0
    $tokenBudgetViolations = 0
    $retrievalLimitViolations = 0
    $omissionContractViolations = 0
    $provenanceIncompleteRuns = 0

    for ($run = 1; $run -le $Runs; $run++) {
        $body = [ordered]@{
            query = $Query
            project = $ProjectName
            profile = $ProfileName
        }
        $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $response = Invoke-ConnectorJson -Method "POST" -Path "/bhm/context/compile" -Body $body -BaseUrl $baseUrl
            $stopwatch.Stop()
            $profilePayload = $response.profile
            $budget = $response.budget
            $retrieval = $response.retrieval
            $omissions = $response.omissions
            $provenance = $response.provenance

            if ($null -eq $profilePayload -or [string]$profilePayload.name -ne $ProfileName) {
                $profileContractMismatches++
            }
            if ($null -eq $budget -or [int]$budget.estimated_tokens -gt [int]$budget.token_budget) {
                $tokenBudgetViolations++
            }
            if ($null -eq $retrieval -or [int]$retrieval.included_count -gt [int]$retrieval.limit) {
                $retrievalLimitViolations++
            }
            if ($null -eq $omissions -or $null -eq $retrieval -or [int]$omissions.count -ne [int]$retrieval.omitted_count) {
                $omissionContractViolations++
            }
            if ($null -eq $provenance -or -not [bool]$provenance.complete) {
                $provenanceIncompleteRuns++
            }

            $samples += [pscustomobject][ordered]@{
                run = $run
                latency_ms = [Math]::Round($stopwatch.Elapsed.TotalMilliseconds, 3)
                profile = if ($null -ne $profilePayload) { [string]$profilePayload.name } else { "" }
                token_budget = if ($null -ne $budget) { [int]$budget.token_budget } else { 0 }
                estimated_tokens = if ($null -ne $budget) { [int]$budget.estimated_tokens } else { 0 }
                candidate_count = if ($null -ne $retrieval) { [int]$retrieval.candidate_count } else { 0 }
                eligible_count = if ($null -ne $retrieval) { [int]$retrieval.eligible_count } else { 0 }
                included_count = if ($null -ne $retrieval) { [int]$retrieval.included_count } else { 0 }
                omitted_count = if ($null -ne $retrieval) { [int]$retrieval.omitted_count } else { 0 }
                truncated = if ($null -ne $budget) { [bool]$budget.truncated } else { $false }
            }
        }
        catch {
            $stopwatch.Stop()
            $message = ($_.Exception.Message -replace "\r?\n", " ")
            if ($message.Length -gt 400) {
                $message = $message.Substring(0, 400)
            }
            $errors += [pscustomobject]@{
                run = $run
                latency_ms = [Math]::Round($stopwatch.Elapsed.TotalMilliseconds, 3)
                error = $message
            }
        }
    }

    $latencies = @($samples | ForEach-Object { [double]$_.latency_ms })
    $p95 = Get-Percentile -Values $latencies -Percentile 0.95
    $successfulRuns = @($samples).Count
    $failedRuns = @($errors).Count
    $latencyThresholdExceeded = if ($null -ne $p95 -and $p95 -gt $MaxLatencyMs) { 1 } else { 0 }
    $omittedTotal = [int](@($samples | Measure-Object -Property omitted_count -Sum).Sum)
    $candidateTotal = [int](@($samples | Measure-Object -Property candidate_count -Sum).Sum)
    $eligibleTotal = [int](@($samples | Measure-Object -Property eligible_count -Sum).Sum)
    $includedTotal = [int](@($samples | Measure-Object -Property included_count -Sum).Sum)
    $truncatedRuns = [int](@($samples | Where-Object { $_.truncated }).Count)
    $passed = (
        $failedRuns -eq 0 -and
        $profileContractMismatches -eq 0 -and
        $tokenBudgetViolations -eq 0 -and
        $retrievalLimitViolations -eq 0 -and
        $omissionContractViolations -eq 0 -and
        $latencyThresholdExceeded -eq 0
    )

    return [pscustomobject][ordered]@{
        ok = [bool]$passed
        passed = [bool]$passed
        base_url = $baseUrl
        profile = $ProfileName
        query = $Query
        requested_runs = $Runs
        successful_runs = $successfulRuns
        failed_runs = $failedRuns
        max_latency_ms = $MaxLatencyMs
        latency_threshold_exceeded = $latencyThresholdExceeded
        latency_ms = [ordered]@{
            min = Get-Percentile -Values $latencies -Percentile 0.0
            p50 = Get-Percentile -Values $latencies -Percentile 0.5
            p95 = $p95
            max = Get-Percentile -Values $latencies -Percentile 1.0
            average = if ($latencies.Count -gt 0) { [Math]::Round((($latencies | Measure-Object -Average).Average), 3) } else { $null }
        }
        profile_contract_mismatches = $profileContractMismatches
        token_budget_violations = $tokenBudgetViolations
        retrieval_limit_violations = $retrievalLimitViolations
        omission_contract_violations = $omissionContractViolations
        provenance_incomplete_runs = $provenanceIncompleteRuns
        candidate_records = $candidateTotal
        eligible_records = $eligibleTotal
        included_records = $includedTotal
        omitted_records = $omittedTotal
        truncated_runs = $truncatedRuns
        samples = @($samples)
        errors = @($errors)
    }
}

$beforeCount = 0
if (Test-Path -LiteralPath $WorkerLogPath) {
    $beforeCount = @(Get-Content -LiteralPath $WorkerLogPath -Encoding UTF8).Count
}

$setJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $setProfileScript -Profile $Profile -RestartWorker -AsJson
$setResult = $setJson | ConvertFrom-Json

$runResult = $null
$artifactPath = $null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

if ($Lightweight) {
    $preflightJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $preflightScript -Project $Project -AsJson
    $preflight = $preflightJson | ConvertFrom-Json

    $mcpJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $showMcpScript -AsJson
    $mcp = $mcpJson | ConvertFrom-Json

    $lightweightArtifact = [ordered]@{
        ok = $true
        mode = "lightweight"
        profile = $Profile
        project = $Project
        title = $Title
        generated_at = (Get-Date).ToString("o")
        preflight = $preflight
        mcp_sources = $mcp
    }

    $artifactPath = Join-Path $ArtifactDir ("lightweight-{0}-{1}.json" -f $Profile, $stamp)
    Write-Utf8Json -Path $artifactPath -Value $lightweightArtifact

    $runResult = [ordered]@{
        ok = $true
        mode = "lightweight"
        artifact = $artifactPath
        preflight_ok = [bool]$preflight.ok
    }
}
else {
    $bundleJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $bundleScript `
        -Project $Project `
        -Title $Title `
        -Done "ran plugin smoke under $Profile profile" `
        -Next "compare error counts for $Profile profile" `
        -Checks "profile applied; worker restarted; plugin bundle generated" `
        -Risks "llm-heavy post-processing may still pressure local model" `
        -AsJson
    $runResult = $bundleJson | ConvertFrom-Json
    $artifactPath = $runResult.latest
}

$runtimeMetrics = Invoke-ContextProfileRuntimeMetrics `
    -ProfileName $Profile `
    -ProjectName $Project `
    -Query $ContextQuery `
    -Runs $ContextRuns `
    -MaxLatencyMs $ContextMaxLatencyMs

$afterLines = @()
if (Test-Path -LiteralPath $WorkerLogPath) {
    $allLines = Get-Content -LiteralPath $WorkerLogPath -Encoding UTF8
    if ($beforeCount -lt $allLines.Count) {
        $afterLines = $allLines[$beforeCount..($allLines.Count - 1)]
    }
}

$tailText = $afterLines -join "`n"
$metrics = [ordered]@{
    context_size_exceeded = ([regex]::Matches($tailText, "Context size has been exceeded")).Count
    circuit_breaker_open = ([regex]::Matches($tailText, "circuit_breaker_open")).Count
    summarize_failed = ([regex]::Matches($tailText, "Summarize failed")).Count
    compression_failed = ([regex]::Matches($tailText, "Compression failed")).Count
    graph_extraction_failed = ([regex]::Matches($tailText, "Graph extraction failed")).Count
}

$workerErrorScore = (
    [int]$metrics.context_size_exceeded +
    [int]$metrics.circuit_breaker_open +
    [int]$metrics.summarize_failed +
    [int]$metrics.compression_failed +
    [int]$metrics.graph_extraction_failed
)

$result = [ordered]@{
    ok = [bool]($setResult.ok -and $runtimeMetrics.passed -and $workerErrorScore -eq 0)
    profile = $Profile
    set_profile = $setResult
    run = $runResult
    metrics = $metrics
    worker_error_score = $workerErrorScore
    runtime_metrics = $runtimeMetrics
    worker_log_delta_lines = @($afterLines).Count
    lightweight = [bool]$Lightweight
    artifact = $artifactPath
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 20
    if ($Strict -and -not $result.ok) {
        exit 1
    }
    exit 0
}

Write-Host "=== BHM Profile Test ==="
Write-Host "- profile : $Profile"
Write-Host "- mode    : $(if ($Lightweight) { 'lightweight' } else { 'full' })"
Write-Host "- output  : $artifactPath"
Write-Host "- context : $($metrics.context_size_exceeded)"
Write-Host "- circuit : $($metrics.circuit_breaker_open)"
Write-Host "- summary : $($metrics.summarize_failed)"
Write-Host "- compress: $($metrics.compression_failed)"
Write-Host "- graph   : $($metrics.graph_extraction_failed)"
Write-Host "- runtime : $($runtimeMetrics.successful_runs)/$($runtimeMetrics.requested_runs) calls; p95=$($runtimeMetrics.latency_ms.p95)ms; passed=$($runtimeMetrics.passed)"
if ($Strict -and -not $result.ok) {
    exit 1
}
