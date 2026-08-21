param(
  [switch]$NoWait,
  [ValidateRange(1, 60)][int]$ShutdownTimeoutSec = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
$workspaceAvailabilityProbeTimeoutSec = Get-BhmOperatorProbeTimeout -Name 'workspace_availability'
$apiParts = Get-BhmRuntimeEndpointParts -Name 'bhm_api' -RepoRoot $repoRoot
$apiAddress = "$($apiParts.Host):$($apiParts.Port)"
$bhmBaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot

function Test-Url {
  param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][int]$TimeoutSec
  )

  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec $TimeoutSec
    return [pscustomobject]@{
      ok = $true
      status = [int]$response.StatusCode
      url = $Url
      error = ""
    }
  } catch {
    $status = $null
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
      $status = [int]$_.Exception.Response.StatusCode
    }
    return [pscustomobject]@{
      ok = $false
      status = $status
      url = $Url
      error = $_.Exception.Message
    }
  }
}

function Get-BhmProcesses {
  try {
    @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
      $_.Name -eq 'python.exe' -and
      $_.CommandLine -match 'uvicorn' -and
      $_.CommandLine -match 'blackholememm?ory\.app:app'
    })
  } catch {
    @()
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

function Stop-BhmPortOwners {
  $listenerIds = @(Get-BhmListeningProcessIds)
  foreach ($listenerId in $listenerIds) {
    if ($listenerId -le 0) {
      continue
    }
    Stop-Process -Id $listenerId -Force -ErrorAction SilentlyContinue
  }
}

function Stop-BhmProcesses {
  $processes = @(Get-BhmProcesses)
  $targetIds = @($processes | ForEach-Object { [int]$_.ProcessId })
  foreach ($proc in $processes) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
  }
  $portOwners = @(Get-BhmListeningProcessIds)
  $targetIds += $portOwners
  foreach ($listenerId in $portOwners) {
    Stop-Process -Id $listenerId -Force -ErrorAction SilentlyContinue
  }
  $targetIds = @($targetIds | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
  $deadline = [DateTime]::UtcNow.AddSeconds($ShutdownTimeoutSec)
  do {
    $remaining = @($targetIds | Where-Object {
        try { $null -ne (Get-Process -Id $_ -ErrorAction Stop) } catch { $false }
      })
    if ($remaining.Count -eq 0) { return }
    Start-Sleep -Milliseconds 250
  } while ([DateTime]::UtcNow -lt $deadline)
  foreach ($processId in $remaining) {
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
  }
  $retryDeadline = [DateTime]::UtcNow.AddSeconds($ShutdownTimeoutSec)
  do {
    $remaining = @($remaining | Where-Object {
        try { $null -ne (Get-Process -Id $_ -ErrorAction Stop) } catch { $false }
      })
    if ($remaining.Count -eq 0) { return }
    Start-Sleep -Milliseconds 250
  } while ([DateTime]::UtcNow -lt $retryDeadline)
  throw "BHM workspace process cleanup exceeded bounded shutdown deadline of $ShutdownTimeoutSec seconds. Remaining PIDs: $($remaining -join ', ')"
}

function Start-DetachedPowerShell {
  param(
    [Parameter(Mandatory = $true)][string]$ScriptPath,
    [string[]]$ExtraArguments = @(),
    [Parameter(Mandatory = $true)][string]$StdoutLog,
    [Parameter(Mandatory = $true)][string]$StderrLog,
    [Parameter(Mandatory = $true)][string]$WorkingDirectory
  )

  $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ScriptPath)
  $arguments += $ExtraArguments
  Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList $arguments `
    -WorkingDirectory $WorkingDirectory `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog | Out-Null
}

$runtimeRoot = Join-Path $repoRoot ".runtime\bootstrap"
$stdoutLog = Join-Path $runtimeRoot "bhm-stdout.log"
$stderrLog = Join-Path $runtimeRoot "bhm-stderr.log"
$qdrantStdoutLog = Join-Path $runtimeRoot "qdrant-stdout.log"
$qdrantStderrLog = Join-Path $runtimeRoot "qdrant-stderr.log"
$startScript = Join-Path $repoRoot "scripts\start-bhm-authoritative.ps1"
$readyUrl = "$bhmBaseUrl/health/ready"

New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

$ready = Test-Url -Url $readyUrl -TimeoutSec $workspaceAvailabilityProbeTimeoutSec
if ($ready.ok -and $ready.status -eq 200) {
  Write-Output "[INFO] BlackHoleMemory Core is already running."
  exit 0
}

$dashboard = Test-Url -Url "$bhmBaseUrl/" -TimeoutSec $workspaceAvailabilityProbeTimeoutSec
if ($dashboard.ok -and $dashboard.status -eq 200) {
  Write-Output "[SUCCESS] BHM Core spawned in background. Track initialization live via $bhmBaseUrl/"
  exit 0
}

Stop-BhmProcesses

foreach ($logPath in @($stdoutLog, $stderrLog, $qdrantStdoutLog, $qdrantStderrLog)) {
  if (Test-Path -LiteralPath $logPath) {
    Remove-Item -LiteralPath $logPath -Force -ErrorAction SilentlyContinue
  }
}

Start-DetachedPowerShell `
  -ScriptPath $startScript `
  -ExtraArguments @("-NoWait") `
  -StdoutLog $stdoutLog `
  -StderrLog $stderrLog `
  -WorkingDirectory $repoRoot

Write-Output "[SUCCESS] BHM Core spawned in background. Track initialization live via $bhmBaseUrl/"
exit 0
