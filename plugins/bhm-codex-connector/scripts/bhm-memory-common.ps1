Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-PluginDataRoot {
    $root = Join-Path $env:USERPROFILE ".codex\plugin-data\bhm"
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    return $root
}

function Get-ConnectorRuntimeConfig {
    $pluginRoot = Split-Path -Parent $PSScriptRoot
    $configPath = Join-Path $pluginRoot "config\runtime-discovery.json"

    if (-not (Test-Path -LiteralPath $configPath)) {
        return [ordered]@{
            envPaths = @("%USERPROFILE%/.bhm/.env")
            apiCandidates = @([string]$env:BHM_BASE_URL) | Where-Object { $_ }
            viewerCandidates = @([string]$env:BHM_VIEWER_URL, [string]$env:BHM_BASE_URL) | Where-Object { $_ }
        }
    }

    return Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
}

function Expand-ConnectorPath {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    return [Environment]::ExpandEnvironmentVariables($Value)
}

function Resolve-ConnectorEnvPath {
    $config = Get-ConnectorRuntimeConfig
    foreach ($candidate in @($config.envPaths)) {
        $expanded = Expand-ConnectorPath ([string]$candidate)
        if ($expanded -and (Test-Path -LiteralPath $expanded)) {
            return $expanded
        }
    }
    return Expand-ConnectorPath "%USERPROFILE%/.bhm/.env"
}

function Get-ConnectorPathVariants {
    param([string]$Path)
    return @($Path)
}

function Read-ConnectorEnv {
    $path = Resolve-ConnectorEnvPath
    $values = @{}
    if (Test-Path -LiteralPath $path) {
        foreach ($rawLine in Get-Content -LiteralPath $path -Encoding UTF8) {
            $line = $rawLine.Trim()
            if (-not $line -or $line.StartsWith("#")) { continue }
            $idx = $line.IndexOf("=")
            if ($idx -le 0) { continue }
            $key = $line.Substring(0, $idx).Trim()
            $value = $line.Substring($idx + 1).Trim()
            $values[$key] = $value
        }
    }
    return [pscustomobject]@{
        path = $path
        values = $values
    }
}

function Get-ConnectorCallerToken {
    $token = [string][Environment]::GetEnvironmentVariable('BHM_CALLER_TOKEN', 'User')
    if ($token.Length -lt 32) { $token = [string]$env:BHM_CALLER_TOKEN }
    if ($token.Length -lt 32) {
        throw 'BHM caller credential is unavailable; start BHM once to initialize BHM_CALLER_TOKEN'
    }
    return $token
}

function Invoke-ConnectorProbe {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 12
        return [ordered]@{ ok = $true; status = [int]$response.StatusCode; url = $Url; reason = "ok" }
    } catch {
        $status = $null
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            $status = [int]$_.Exception.Response.StatusCode
        }
        return [ordered]@{ ok = $false; status = $status; url = $Url; reason = $_.Exception.Message }
    }
}

function Resolve-ConnectorBaseUrl {
    $config = Get-ConnectorRuntimeConfig
    $envInfo = Read-ConnectorEnv
    $env = $envInfo.values
    $candidates = @()
    if ($env:BHM_WORKSPACE_MEMORY_URL) { $candidates += [string]$env:BHM_WORKSPACE_MEMORY_URL }
    if ($env.ContainsKey("III_REST_PORT")) { $candidates += "http://localhost:$($env["III_REST_PORT"])" }
    $candidates += @($config.apiCandidates)
    $candidates = @($candidates | Where-Object { $_ } | Select-Object -Unique)

    foreach ($candidate in $candidates) {
        $probe = Invoke-ConnectorProbe -Url "$candidate/bhm/health"
        if ($probe.ok -and $probe.status -eq 200) {
            return $candidate
        }
    }
    return ($candidates | Select-Object -First 1)
}

function Resolve-ConnectorViewerUrl {
    $config = Get-ConnectorRuntimeConfig
    $envInfo = Read-ConnectorEnv
    $env = $envInfo.values
    $candidates = @()
    $candidates += @($config.viewerCandidates)
    $candidates = @($candidates | Where-Object { $_ } | Select-Object -Unique)

    foreach ($candidate in $candidates) {
        $probe = Invoke-ConnectorProbe -Url $candidate
        if ($probe.ok -and $probe.status -eq 200) {
            return $candidate
        }
    }
    return ($candidates | Select-Object -First 1)
}

function Invoke-ConnectorJson {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [object]$Body = $null,
        [hashtable]$Query = $null,
        [string]$BaseUrl = $(Resolve-ConnectorBaseUrl)
    )

    Add-Type -AssemblyName System.Net.Http
    $client = [System.Net.Http.HttpClient]::new()
    try {
        $client.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new(
            'Bearer',
            (Get-ConnectorCallerToken)
        )
        $lastFailure = $null
        foreach ($pathVariant in (Get-ConnectorPathVariants -Path $Path)) {
            $uri = "$BaseUrl$pathVariant"
            if ($Query -and $Query.Count -gt 0) {
                $pairs = @()
                foreach ($entry in $Query.GetEnumerator()) {
                    if ($null -eq $entry.Value) { continue }
                    $value = [string]$entry.Value
                    if ([string]::IsNullOrWhiteSpace($value)) { continue }
                    $pairs += ("{0}={1}" -f [uri]::EscapeDataString([string]$entry.Key), [uri]::EscapeDataString($value))
                }
                if ($pairs.Count -gt 0) {
                    $uri = "{0}?{1}" -f $uri, ($pairs -join "&")
                }
            }

            if ($null -ne $Body) {
                $json = $Body | ConvertTo-Json -Depth 30 -Compress
                $content = [System.Net.Http.StringContent]::new(
                    $json,
                    [System.Text.UTF8Encoding]::new($false),
                    "application/json"
                )
                switch ($Method.ToUpperInvariant()) {
                    "POST" { $response = $client.PostAsync($uri, $content).GetAwaiter().GetResult(); break }
                    default { throw "Unsupported method with body: $Method" }
                }
            } else {
                switch ($Method.ToUpperInvariant()) {
                    "GET" { $response = $client.GetAsync($uri).GetAwaiter().GetResult(); break }
                    "DELETE" { $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Delete, $uri); $response = $client.SendAsync($request).GetAwaiter().GetResult(); break }
                    default { throw "Unsupported method without body: $Method" }
                }
            }

            $text = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            if ($response.IsSuccessStatusCode) {
                if ([string]::IsNullOrWhiteSpace($text)) { return $null }
                return $text | ConvertFrom-Json
            }

            $lastFailure = "$Method $pathVariant failed: HTTP $([int]$response.StatusCode) $text"
            if ($response.StatusCode -ne [System.Net.HttpStatusCode]::NotFound) {
                throw $lastFailure
            }
        }

        if ($lastFailure) {
            throw $lastFailure
        }
        throw "$Method $Path failed: no compatible route variant responded"
    } finally {
        $client.Dispose()
    }
}

function Get-ConnectorMcpAttachStatus {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl
    )

    $normalizedBaseUrl = $BaseUrl.TrimEnd('/')
    $httpEnvelope = $null
    $httpStatus = $null
    $httpError = $null
    try {
        $httpEnvelope = Invoke-ConnectorJson -Method "GET" -Path "/bhm/mcp/http/status" -BaseUrl $normalizedBaseUrl
        if ($null -ne $httpEnvelope.sessions) { $httpStatus = $httpEnvelope.sessions }
    } catch {
        $httpError = $_.Exception.Message
    }

    $httpContractValid = (
        $null -ne $httpEnvelope -and
        [string]$httpEnvelope.transport -eq "streamable_http" -and
        $null -ne $httpStatus
    )
    $httpAttached = if ($httpContractValid -and $null -ne $httpStatus.attached_count) { [int]$httpStatus.attached_count } else { 0 }
    $httpPending = if ($httpContractValid -and $null -ne $httpStatus.pending_count) { [int]$httpStatus.pending_count } else { 0 }
    $httpStatusValue = if ($httpContractValid -and [string]$httpStatus.status -in @("attached", "pending", "detached")) { [string]$httpStatus.status } else { "unavailable" }
    $streamableHttpReady = $httpContractValid
    $probeOk = $httpContractValid
    $transportReady = $streamableHttpReady
    $statusValue = if ($httpAttached -gt 0) { "attached" } elseif ($httpPending -gt 0) { "pending" } elseif ($probeOk) { "detached" } else { "unavailable" }
    $httpReasonCode = if ($httpContractValid) {
        if ($httpAttached -gt 0) { "ok" } else { "no_live_session" }
    } elseif ($null -ne $httpEnvelope) {
        "contract_invalid"
    } else {
        "probe_failed"
    }
    $transports = [ordered]@{
        streamable_http = [ordered]@{
            transport = "streamable_http"
            probe_ok = [bool]$httpContractValid
            ready = [bool]$streamableHttpReady
            status = $httpStatusValue
            attached_count = $httpAttached
            pending_count = $httpPending
            expired_count = 0
            reason_code = $httpReasonCode
        }
    }

    return [ordered]@{
        ok = [bool]$probeOk
        schema_version = if ($probeOk) { "bhm.mcp.streamable-http-only.v1" } else { "" }
        status = $statusValue
        transport_ready = [bool]$transportReady
        streamable_http_ready = [bool]$streamableHttpReady
        attached_count = $httpAttached
        pending_count = $httpPending
        expired_count = 0
        transports = $transports
        reason = $httpError
    }
}

function New-ConnectorTransportTruth {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [string]$Operation = "ritual",
        [bool]$RestBridgeUsed = $true,
        [bool]$RestBridgeSucceeded = $true
    )

    $attach = Get-ConnectorMcpAttachStatus -BaseUrl $BaseUrl
    $runtimeLeaseLive = $attach.ok -and $attach.status -eq "attached" -and $attach.attached_count -gt 0
    $transportReady = $attach.ok -and [bool]$attach.transport_ready
    $streamableHttpReady = $attach.ok -and [bool]$attach.streamable_http_ready
    $reasonCode = if (-not $attach.ok) {
        "attach_status_probe_failed"
    } elseif ($runtimeLeaseLive) {
        "current_session_unverified"
    } elseif ($streamableHttpReady) {
        "streamable_http_idle_or_detached"
    } else {
        "no_live_native_lease"
    }
    $status = if ($runtimeLeaseLive) {
        "native MCP live; current session unverified"
    } elseif ($streamableHttpReady) {
        "native MCP transport ready; session idle or detached"
    } else {
        "MCP unavailable"
    }
    $recoveryAction = if ($runtimeLeaseLive) {
        "verify this client with a native BHM tool call; the REST wrapper cannot prove session identity; reload only if the native probe fails"
    } elseif ($streamableHttpReady) {
        "invoke a native BHM tool to establish or recover the Streamable HTTP session; reload only if the native probe fails while runtime is healthy"
    } else {
        "start or repair the canonical BHM transport and re-probe; reload only after runtime/config repair; do not replay failed MCP tool calls"
    }

    return [ordered]@{
        schema_version = "bhm.mcp.rest-degraded.v1"
        path = "rest-bridge"
        operation = $Operation
        status = $status
        degraded = $true
        mcp_available = $false
        native_mcp = [ordered]@{
            attached = $false
            current_session_verified = $false
            runtime_lease_live = [bool]$runtimeLeaseLive
            transport_ready = [bool]$transportReady
            streamable_http_ready = [bool]$streamableHttpReady
            probe_ok = [bool]$attach.ok
            reason_code = $reasonCode
            attached_count = [int]$attach.attached_count
            pending_count = [int]$attach.pending_count
            expired_count = [int]$attach.expired_count
            transports = $attach.transports
        }
        rest_bridge = [ordered]@{
            used = [bool]$RestBridgeUsed
            available = [bool]$RestBridgeSucceeded
            base_url = $BaseUrl.TrimEnd('/')
        }
        retry = [ordered]@{
            native_attempts = 0
            native_retries = 0
            failed_tool_call_loop = $false
            policy = "no-native-retry"
        }
        recovery_action = $recoveryAction
    }
}

function Get-ConnectorSessionDir {
    param([string]$Project)
    $root = Join-Path (Get-PluginDataRoot) "runtime\logs"
    if ($Project -eq "e-github-workspace") {
        $path = Join-Path $root "_workspace\sessions"
    } else {
        $path = Join-Path $root "projects\$Project\sessions"
    }
    New-Item -ItemType Directory -Force -Path $path | Out-Null
    return $path
}

function ConvertTo-ConnectorSlug {
    param([string]$Value)
    $slug = ($Value -replace '[^A-Za-z0-9._-]+', '-').Trim("-")
    if ([string]::IsNullOrWhiteSpace($slug)) { return "session" }
    return $slug.ToLowerInvariant()
}
