param(
  [string]$BaseUrl = '',
  [switch]$AsJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
. (Join-Path $repoRoot 'scripts\bhm-caller-credential.ps1')
if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
  $BaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot
}
$credential = Initialize-BhmCallerCredential
$headers = @{ Authorization = "Bearer $env:BHM_CALLER_TOKEN" }

$StreamableHttpTimeoutSec = 10
$StreamableHttpMaxResponseBytes = 262144

function Assert-StreamableValidatorUri {
  param([Parameter(Mandatory = $true)][string]$Candidate)

  $parsed = $null
  if (-not [Uri]::TryCreate($Candidate, [UriKind]::Absolute, [ref]$parsed)) {
    throw 'streamable HTTP validator URL is not an absolute URI'
  }
  $allowedHosts = @('127.0.0.1', 'localhost', '::1')
  if ($parsed.Scheme -ne 'http' -or $allowedHosts -notcontains $parsed.Host.ToLowerInvariant()) {
    throw 'streamable HTTP validator requires an HTTP loopback endpoint'
  }
  if (-not [string]::IsNullOrWhiteSpace($parsed.UserInfo)) {
    throw 'streamable HTTP validator URL must not contain userinfo'
  }
  if (-not [string]::IsNullOrWhiteSpace($parsed.Fragment)) {
    throw 'streamable HTTP validator URL must not contain a fragment'
  }
  return $parsed
}

function Invoke-StreamableValidatorJson {
  param(
    [Parameter(Mandatory = $true)][string]$Uri,
    [hashtable]$RequestHeaders = @{}
  )

  $parsed = Assert-StreamableValidatorUri -Candidate $Uri
  $handler = [System.Net.Http.HttpClientHandler]::new()
  $handler.AllowAutoRedirect = $false
  $handler.UseProxy = $false
  $client = [System.Net.Http.HttpClient]::new($handler)
  $client.Timeout = [TimeSpan]::FromSeconds($StreamableHttpTimeoutSec)
  $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $parsed)
  $response = $null
  try {
    foreach ($key in $RequestHeaders.Keys) {
      $request.Headers.TryAddWithoutValidation([string]$key, [string]$RequestHeaders[$key]) | Out-Null
    }
    $response = $client.SendAsync(
      $request,
      [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
    ).GetAwaiter().GetResult()
    if (-not $response.IsSuccessStatusCode) {
      throw "streamable HTTP validator returned HTTP $([int]$response.StatusCode)"
    }
    $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
    if ($bytes.Length -gt $StreamableHttpMaxResponseBytes) {
      throw "streamable HTTP validator response exceeded bounded limit $StreamableHttpMaxResponseBytes bytes"
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

function Get-Json([string]$Path, [hashtable]$RequestHeaders = @{}) {
  Invoke-StreamableValidatorJson -Uri "$($BaseUrl.TrimEnd('/'))$Path" -RequestHeaders $RequestHeaders
}

$health = Get-Json '/bhm/health'
$cutover = Get-Json '/health/cutover'
$slo = Get-Json '/bhm/health/slo'
$http = Get-Json '/bhm/mcp/http/status' $headers
$openapi = Get-Json '/openapi.json'
$legacyPaths = @($openapi.paths.PSObject.Properties.Name | Where-Object { $_ -match '/bhm/mcp/(attach|connection|telemetry/mcp-attach)' })
$sessions = $http.sessions
$checks = [ordered]@{
  runtime_healthy = $health.status -eq 'healthy'
  cutover_ready = [bool]$cutover.ok
  slo_healthy = $slo.status -eq 'healthy'
  streamable_contract = [string]$http.transport -eq 'streamable_http' -and [string]$http.server_id -eq 'bhm'
  streamable_sessions_bounded = ([int]$sessions.max_sessions -gt 0 -and [double]$sessions.idle_seconds -gt 0)
  legacy_public_paths_removed = $legacyPaths.Count -eq 0
  health_uses_streamable_truth = $null -ne $health.mcp_transport -and [string]$health.mcp_transport.authoritative_source -eq 'streamable_http_sessions'
}
$result = [ordered]@{
  schema_version = 'bhm.mcp.streamable-http-only-validation.v1'
  ok = [bool]($checks.Values -notcontains $false)
  base_url = $BaseUrl.TrimEnd('/')
  transport = $http.transport
  sessions = $sessions
  legacy_public_paths = $legacyPaths
  checks = $checks
  writes_live_state = $false
  rollback = 'source/package backup retained; no stdio runtime rollback'
}
if ($AsJson) { $result | ConvertTo-Json -Depth 12; if ($result.ok) { exit 0 }; exit 1 }
$result | ConvertTo-Json -Depth 12
if ($result.ok) { exit 0 }
exit 1
