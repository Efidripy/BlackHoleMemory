param(
    [ValidateSet("status", "dry-run", "drain")][string]$Action = "status",
    [string]$BaseUrl = '',
    [ValidateRange(1, 128)][int]$MaxCycles = 32,
    [switch]$AsJson,
    [switch]$SemanticFusion
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Add-Type -AssemblyName System.Net.Http
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
$apiParts = Get-BhmRuntimeEndpointParts -Name 'bhm_api' -RepoRoot $repoRoot
$lmStudioParts = Get-BhmRuntimeEndpointParts -Name 'lm_studio' -RepoRoot $repoRoot
$lmStudioUrl = Get-BhmRuntimeEndpoint -Name 'lm_studio' -RepoRoot $repoRoot
$llmDefaultUrl = Get-BhmRuntimeEndpoint -Name 'llm_default' -RepoRoot $repoRoot
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot }

$ProjectionHttpTimeoutSec = 10
$ProjectionHttpMaxResponseBytes = 262144
$ProjectionShutdownTimeoutSec = 5

function Assert-ProjectionOperatorUri {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    $parsed = $null
    if (-not [Uri]::TryCreate($Candidate, [UriKind]::Absolute, [ref]$parsed)) {
        throw 'projection operator probe URL is not an absolute URI'
    }
    $allowedHosts = @('127.0.0.1', 'localhost', '::1')
    if ($parsed.Scheme -ne 'http' -or $allowedHosts -notcontains $parsed.Host.ToLowerInvariant()) {
        throw 'projection operator probes require an HTTP loopback endpoint'
    }
    if (-not [string]::IsNullOrWhiteSpace($parsed.UserInfo)) {
        throw 'projection operator probe URL must not contain userinfo'
    }
    if (-not [string]::IsNullOrWhiteSpace($parsed.Query) -or -not [string]::IsNullOrWhiteSpace($parsed.Fragment)) {
        throw 'projection operator probe URL must not contain query or fragment'
    }
    return $parsed
}

function Invoke-ProjectionOperatorJson {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [hashtable]$Headers = @{}
    )

    $parsed = Assert-ProjectionOperatorUri -Candidate $Uri
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $handler.UseProxy = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($ProjectionHttpTimeoutSec)
    $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $parsed)
    $response = $null
    try {
        foreach ($key in $Headers.Keys) {
            $request.Headers.TryAddWithoutValidation([string]$key, [string]$Headers[$key]) | Out-Null
        }
        $response = $client.SendAsync(
            $request,
            [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw "projection operator probe returned HTTP $([int]$response.StatusCode)"
        }
        $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        if ($bytes.Length -gt $ProjectionHttpMaxResponseBytes) {
            throw "projection operator probe response exceeded bounded limit $ProjectionHttpMaxResponseBytes bytes"
        }
        return ([Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json)
    }
    finally {
        if ($null -ne $response) { $response.Dispose() }
        $request.Dispose()
        $client.Dispose()
        $handler.Dispose()
    }
}

function Get-ConfiguredOpenAiBaseUrl {
    if (-not [string]::IsNullOrWhiteSpace([string]$env:OPENAI_BASE_URL)) {
        return ([string]$env:OPENAI_BASE_URL).Trim().TrimEnd('/')
    }

    $envPath = Join-Path $HOME '.bhm\.env'
    if (-not (Test-Path -LiteralPath $envPath)) { return '' }

    foreach ($line in Get-Content -LiteralPath $envPath -ErrorAction SilentlyContinue) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#') -or $trimmed.IndexOf('=') -lt 1) {
            continue
        }
        $parts = $trimmed.Split('=', 2)
        if ($parts[0].Trim() -eq 'OPENAI_BASE_URL') {
            return $parts[1].Split('#', 2)[0].Trim().TrimEnd('/')
        }
    }
    return ''
}

function Test-OpenAiBaseUrl {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    try {
        $null = Invoke-ProjectionOperatorJson -Uri "$($Candidate.TrimEnd('/'))/models"
        return $true
    } catch {
        return $false
    }
}

function Resolve-LocalLmStudioEndpoint {
    $configured = Get-ConfiguredOpenAiBaseUrl
    $loopback = $lmStudioUrl
    $staleDockerHost = $configured -match ('^https?://172\.18\.0\.1:' + $lmStudioParts.Port + '/v1/?$')
    $defaultEndpoint = [string]::IsNullOrWhiteSpace($configured) -or $configured -eq $llmDefaultUrl

    # Projection is a local, explicit recovery path. If no provider override is
    # present (or the configured default is unavailable), prefer the live
    # catalogued LM Studio endpoint. Explicit live custom endpoints remain
    # untouched, while the old Docker-host value is normalized to loopback.
    if (($staleDockerHost -or $defaultEndpoint) -and (Test-OpenAiBaseUrl -Candidate $loopback)) {
        $env:OPENAI_BASE_URL = $loopback
        $env:BHM_MEM0_OPENAI_BASE_URL = $loopback
    }
}

function Get-BhmProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'python.exe' -and
        $_.CommandLine -match 'uvicorn' -and
        $_.CommandLine -match 'blackholememm?ory\.app:app'
    })
}

function Stop-BhmProcesses {
    $processes = @(Get-BhmProcesses)
    $knownIds = @($processes | ForEach-Object { [int]$_.ProcessId })
    foreach ($proc in $processes) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    $listeners = netstat -ano | Select-String ([regex]::Escape("$($apiParts.Host):$($apiParts.Port)"))
    foreach ($line in $listeners) {
        $parts = ($line.ToString() -split '\s+') | Where-Object { $_ }
        if ($parts.Length -lt 5 -or $parts[3] -ne 'LISTENING') { continue }
        $listenerId = 0
        if ([int]::TryParse($parts[4], [ref]$listenerId) -and $knownIds -contains $listenerId) {
            Stop-Process -Id $listenerId -Force -ErrorAction SilentlyContinue
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($ProjectionShutdownTimeoutSec)
    do {
        $remaining = @($knownIds | Where-Object {
            try { $null -ne (Get-Process -Id $_ -ErrorAction Stop) } catch { $false }
        })
        if ($remaining.Count -eq 0) { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    foreach ($processId in $remaining) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    $retryDeadline = [DateTime]::UtcNow.AddSeconds($ProjectionShutdownTimeoutSec)
    do {
        $remaining = @($remaining | Where-Object {
            try { $null -ne (Get-Process -Id $_ -ErrorAction Stop) } catch { $false }
        })
        if ($remaining.Count -eq 0) { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $retryDeadline)
    throw "Projection operator process cleanup exceeded bounded shutdown deadline of $ProjectionShutdownTimeoutSec seconds. Remaining PIDs: $($remaining -join ', ')"
}

function Get-BhmCallerHeaders {
    $token = [string][Environment]::GetEnvironmentVariable('BHM_CALLER_TOKEN', 'Process')
    if ([string]::IsNullOrWhiteSpace($token)) {
        $token = [string][Environment]::GetEnvironmentVariable('BHM_CALLER_TOKEN', 'User')
    }
    if ([string]::IsNullOrWhiteSpace($token)) {
        $envPath = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.bhm\.env'
        foreach ($line in Get-Content -LiteralPath $envPath -ErrorAction SilentlyContinue) {
            if ($line -match '^\s*BHM_CALLER_TOKEN\s*=') {
                $token = $line.Split('=', 2)[1].Split('#', 2)[0].Trim().Trim('"').Trim("'")
                break
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace($token) -or $token.Trim().Length -lt 32) {
        throw 'BHM_CALLER_TOKEN is unavailable'
    }
    return @{
        Authorization = "Bearer $($token.Trim())"
        'X-BHM-Caller-Surface' = 'projection-operator'
    }
}

function Get-LiveSlo {
    param([Parameter(Mandatory = $true)][string]$Url)

    $headers = Get-BhmCallerHeaders
    $health = Invoke-ProjectionOperatorJson -Uri "$($Url.TrimEnd('/'))/bhm/health" -Headers $headers
    $cutover = Invoke-ProjectionOperatorJson -Uri "$($Url.TrimEnd('/'))/health/cutover" -Headers $headers
    $slo = Invoke-ProjectionOperatorJson -Uri "$($Url.TrimEnd('/'))/bhm/health/slo" -Headers $headers
    [pscustomobject]@{
        health = $health.status
        version = $health.version
        memory_store = $health.memory_store.backend
        ready = [bool]$health.memory_store.ready
        parity = [bool]$health.memory_store.parity_confirmed
        writer_offline = [bool]$health.memory_store.writer_offline_confirmed
        worker_enabled = [bool]$health.memory_store.projection_worker.enabled
        cutover = [bool]$cutover.ok
        mem0_status = $cutover.mem0.status
        direct_vector_writes = [bool]$cutover.mem0.direct_vector_writes
        slo = $slo.status
        projection_pending = [int]$slo.observed.projection_pending
        projection_failed = [int]$slo.observed.projection_failed
        outbox_completed = [int]$slo.observed.outbox.completed
        outbox_total = [int]$slo.observed.outbox.total
        outbox_dead_letter = [int]$slo.observed.outbox.dead_letter
    }
}

function Get-LiveSloWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [ValidateRange(1, 180)][int]$MaxAttempts = 30,
        [ValidateRange(1, 5)][int]$DelaySeconds = 1
    )

    $lastError = ""
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            return Get-LiveSlo -Url $Url
        } catch {
            $lastError = $_.Exception.Message
            if ($attempt -lt $MaxAttempts) {
                Start-Sleep -Seconds $DelaySeconds
            }
        }
    }
    throw "BHM SLO unavailable after $MaxAttempts attempts: $lastError"
}

function Invoke-WorkerDryRun {
    param([Parameter(Mandatory = $true)][string]$RepoRoot)

    $env:BHM_MEMORY_STORE_MODE = "sqlite-authoritative"
    $env:BHM_PROJECTION_WORKER_ENABLED = "false"
    Resolve-LocalLmStudioEndpoint
    $output = @(python (Join-Path $RepoRoot "scripts\run-bhm-projection-worker.py") --dry-run 2>&1)
    $exitCode = $LASTEXITCODE
    $report = (($output -join [Environment]::NewLine) | ConvertFrom-Json)
    $pending = if ($null -eq $report.outbox.outbox.pending) { 0 } else { [int]$report.outbox.outbox.pending }
    [pscustomobject]@{ ok = ($exitCode -eq 0 -and [bool]$report.ok); exit_code = $exitCode; writes_live_state = [bool]$report.writes_live_state; pending = $pending; report = $report }
}

function Start-CanonicalAuthoritative {
    param([Parameter(Mandatory = $true)][string]$RepoRoot)

$stdout = Join-Path $RepoRoot ".runtime\bootstrap\projection-operator-launcher.stdout.log"
$stderr = Join-Path $RepoRoot ".runtime\bootstrap\projection-operator-launcher.stderr.log"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $stdout) | Out-Null
    $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $RepoRoot "scripts\start-bhm-authoritative.ps1"), "-NoWait")
    if ($SemanticFusion) { $arguments += "-SemanticFusion" }
    $launcher = Start-Process -FilePath "powershell.exe" `
        -ArgumentList $arguments `
        -WorkingDirectory $RepoRoot -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    while (-not $launcher.HasExited -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Seconds 1
        $launcher.Refresh()
    }
    $timedOut = -not $launcher.HasExited
    if ($timedOut) {
        Stop-Process -Id $launcher.Id -Force -ErrorAction SilentlyContinue
        $stopDeadline = [DateTime]::UtcNow.AddSeconds($ProjectionShutdownTimeoutSec)
        do {
            try { $launcher.Refresh() } catch { break }
            if ($launcher.HasExited) { break }
            Start-Sleep -Milliseconds 250
        } while ([DateTime]::UtcNow -lt $stopDeadline)
    }
    $launcher.Refresh()
    $text = if (Test-Path -LiteralPath $stdout) { Get-Content -LiteralPath $stdout -Raw } else { "" }
    $errorText = if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Raw } else { "" }
    $safeText = if ($null -eq $text) { "" } else { [string]$text }
    $safeErrorText = if ($null -eq $errorText) { "" } else { [string]$errorText }
    [pscustomobject]@{
        ok = ($launcher.HasExited -and -not $timedOut -and [string]::IsNullOrWhiteSpace($safeErrorText) -and $safeText -match '"ok"\s*:\s*true')
        exited = $launcher.HasExited
        timed_out = $timedOut
        stdout = $safeText.Trim()
        stderr = $safeErrorText.Trim()
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
try {
    $initial = Get-LiveSlo -Url $BaseUrl
    if ($Action -eq "status") {
        $result = [pscustomobject]@{ ok = $true; action = $Action; before = $initial }
    } elseif ($Action -eq "dry-run") {
        $dryRun = Invoke-WorkerDryRun -RepoRoot $repoRoot
        $result = [pscustomobject]@{ ok = ($dryRun.ok -and $initial.memory_store -eq "sqlite-authoritative"); action = $Action; before = $initial; dry_run = $dryRun }
    } else {
        if ($initial.memory_store -ne "sqlite-authoritative" -or -not $initial.ready -or -not $initial.cutover -or -not $initial.parity -or -not $initial.writer_offline) {
            throw "drain requires a ready sqlite-authoritative runtime with parity and writer-offline guards"
        }
        Stop-BhmProcesses
        Start-Sleep -Seconds 2
        $env:BHM_MEMORY_STORE_MODE = "sqlite-shadow"
        $env:BHM_PROJECTION_WORKER_ENABLED = "true"
        $env:BHM_MEMORY_STORE_PARITY_CONFIRMED = "false"
        $env:BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED = "true"
        $env:BHM_FALLBACK_MODE = "explicit"
        Resolve-LocalLmStudioEndpoint
        $workerOutput = @(python (Join-Path $repoRoot "scripts\run-bhm-projection-worker.py") --loop --max-cycles $MaxCycles --force --openai-base-url $lmStudioUrl 2>&1)
        $workerExit = $LASTEXITCODE
        $workerText = ($workerOutput -join [Environment]::NewLine)
        $workerReport = $null
        try { $workerReport = $workerText | ConvertFrom-Json } catch { }
        $launcher = Start-CanonicalAuthoritative -RepoRoot $repoRoot
        $after = Get-LiveSloWithRetry -Url $BaseUrl -MaxAttempts 120 -DelaySeconds 1
        $result = [pscustomobject]@{
            ok = ($workerExit -eq 0 -and $null -ne $workerReport -and [bool]$workerReport.ok -and $launcher.ok -and $after.slo -eq "healthy" -and $after.projection_pending -eq 0 -and $after.projection_failed -eq 0)
            action = $Action
            max_cycles = $MaxCycles
            before = $initial
            worker = [pscustomobject]@{ exit_code = $workerExit; report = $workerReport; raw = if ($null -eq $workerReport) { $workerText } else { "" } }
            launcher = $launcher
            after = $after
        }
    }
} catch {
    $result = [pscustomobject]@{ ok = $false; action = $Action; error = $_.Exception.Message }
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 12
} else {
    $result | ConvertTo-Json -Depth 12
}
if ($result.ok) { exit 0 }
exit 1
