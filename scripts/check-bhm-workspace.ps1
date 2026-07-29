Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
$apiParts = Get-BhmRuntimeEndpointParts -Name 'bhm_api' -RepoRoot $repoRoot
$apiAddress = "$($apiParts.Host):$($apiParts.Port)"
$qdrantHealthUrl = Get-BhmRuntimeEndpoint -Name 'qdrant_http' -RepoRoot $repoRoot -Path 'healthz'
$bhmBaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot

function Test-Url {
  param(
    [Parameter(Mandatory = $true)][string]$Url,
    [int]$TimeoutSec = 12
  )

  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec $TimeoutSec
    return [pscustomobject]@{
      ok = $true
      status = [int]$response.StatusCode
      body = $response.Content
      url = $Url
    }
  } catch {
    $status = $null
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
      $status = [int]$_.Exception.Response.StatusCode
    }
    return [pscustomobject]@{
      ok = $false
      status = $status
      body = $null
      url = $Url
      error = $_.Exception.Message
    }
  }
}

function Get-BhmProcesses {
  Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and
    $_.CommandLine -match 'uvicorn' -and
    $_.CommandLine -match 'blackholememm?ory\.app:app'
  }
}

function Get-BhmListeningProcessIds {
  $matches = netstat -ano | Select-String ([regex]::Escape($apiAddress))
  $ids = @()
  foreach ($line in $matches) {
    $parts = ($line.ToString() -split '\s+') | Where-Object { $_ }
    if ($parts.Length -lt 5) {
      continue
    }
    if ($parts[0] -ne 'TCP') {
      continue
    }
    if ($parts[1] -ne $apiAddress) {
      continue
    }
    if ($parts[3] -ne 'LISTENING') {
      continue
    }
    $listenerPid = 0
    if ([int]::TryParse($parts[4], [ref]$listenerPid) -and $listenerPid -gt 0) {
      $ids += $listenerPid
    }
  }
  return @($ids | Sort-Object -Unique)
}

$qdrant = Test-Url -Url $qdrantHealthUrl
$bhmReady = Test-Url -Url "$bhmBaseUrl/health/ready"
$bhmHealth = Test-Url -Url "$bhmBaseUrl/bhm/health"
$processes = @(Get-BhmProcesses)
$listenerIds = @(Get-BhmListeningProcessIds)

[pscustomobject]@{
  qdrant = [pscustomobject]@{
    ok = $qdrant.ok
    status = $qdrant.status
    url = $qdrant.url
  }
  bhm = [pscustomobject]@{
    ready_ok = $bhmReady.ok
    ready_status = $bhmReady.status
    health_ok = $bhmHealth.ok
    health_status = $bhmHealth.status
    process_count = $processes.Count
    process_ids = @($processes | Select-Object -ExpandProperty ProcessId)
    listening_process_count = $listenerIds.Count
    listening_process_ids = $listenerIds
  }
  overall_ok = ($qdrant.ok -and $bhmReady.ok -and $bhmHealth.ok -and $listenerIds.Count -eq 1)
} | ConvertTo-Json -Depth 6
