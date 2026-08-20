param(
  [switch]$Force,
  [switch]$WhatIf,
  [ValidateRange(5, 300)][int]$TimeoutSec = 120,
  [ValidateRange(1, 10)][int]$PollSeconds = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
$composeFile = Join-Path $repoRoot 'infra\qdrant\docker-compose.yml'
$qdrantHealthUrl = Get-BhmRuntimeEndpoint -Name 'qdrant_http' -RepoRoot $repoRoot -Path 'healthz'
$runtimeRoot = Join-Path $repoRoot '.runtime\bootstrap'
$receiptPath = Join-Path $runtimeRoot ("qdrant-recovery-{0}.json" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
$steps = [System.Collections.Generic.List[object]]::new()
$errorMessage = ''

function Add-Step {
  param(
    [Parameter(Mandatory)][string]$Name,
    [Parameter(Mandatory)][string]$Status,
    [string]$Detail = ''
  )
  $steps.Add([pscustomobject]@{
      name = $Name
      status = $Status
      detail = $Detail.Substring(0, [Math]::Min($Detail.Length, 500))
      at = [DateTime]::UtcNow.ToString('o')
    })
}

function Resolve-DockerExecutable {
  $command = Get-Command docker -ErrorAction SilentlyContinue
  if ($null -ne $command) { return $command.Source }
  foreach ($candidate in @(
      (Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'),
      (Join-Path $env:LOCALAPPDATA 'Docker\Docker\resources\bin\docker.exe')
    )) {
    if (Test-Path -LiteralPath $candidate) { return $candidate }
  }
  throw 'Docker executable was not found on PATH or in Docker Desktop installation paths.'
}

$dockerExecutable = Resolve-DockerExecutable

function Invoke-DockerBounded {
  param(
    [Parameter(Mandatory)][string[]]$Arguments,
    [ValidateRange(1, 120)][int]$TimeoutSeconds = 20
  )
  $psi = [System.Diagnostics.ProcessStartInfo]::new()
  $psi.FileName = $dockerExecutable
  $psi.Arguments = ($Arguments | ForEach-Object {
      $value = [string]$_
      if ($value -match '[\s"]') { '"' + $value.Replace('"', '\\"') + '"' } else { $value }
    }) -join ' '
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $process = [System.Diagnostics.Process]::new()
  $process.StartInfo = $psi
  if (-not $process.Start()) { throw 'Docker process could not be started.' }
  if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
    try { $process.Kill($true) } catch { }
    return [pscustomobject]@{ timed_out = $true; exit_code = $null; output = ''; error = "docker command timed out after $TimeoutSeconds seconds" }
  }
  return [pscustomobject]@{
    timed_out = $false
    exit_code = $process.ExitCode
    output = $process.StandardOutput.ReadToEnd()
    error = $process.StandardError.ReadToEnd()
  }
}

function Test-DockerReady {
  $result = Invoke-DockerBounded -Arguments @('info', '--format', '{{.ServerVersion}}') -TimeoutSeconds 3
  $detail = (@($result.output, $result.error) | Where-Object { $_ }) -join '; '
  return [pscustomobject]@{ ready = (-not $result.timed_out) -and $result.exit_code -eq 0; detail = $detail }
}

function Test-QdrantReady {
  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $qdrantHealthUrl -TimeoutSec 5
    return [pscustomobject]@{ ready = [int]$response.StatusCode -eq 200; detail = "HTTP $([int]$response.StatusCode)" }
  } catch {
    return [pscustomobject]@{ ready = $false; detail = $_.Exception.Message }
  }
}

function Wait-QdrantReady {
  do {
    $probe = Test-QdrantReady
    if ($probe.ready) { return $probe }
    if ([DateTime]::UtcNow -lt $deadline) { Start-Sleep -Seconds $PollSeconds }
  } while ([DateTime]::UtcNow -lt $deadline)
  return (Test-QdrantReady)
}

function Start-DockerDesktopBounded {
  $desktopPath = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
  if (-not (Test-Path -LiteralPath $desktopPath)) { throw 'Docker Desktop executable was not found.' }
  Start-Process -FilePath $desktopPath -WindowStyle Hidden | Out-Null
  do {
    $probe = Test-DockerReady
    if ($probe.ready) { return $probe }
    if ([DateTime]::UtcNow -lt $deadline) { Start-Sleep -Seconds $PollSeconds }
  } while ([DateTime]::UtcNow -lt $deadline)
  return (Test-DockerReady)
}

function Start-QdrantCompose {
  if (-not (Test-Path -LiteralPath $composeFile)) { throw "Qdrant compose file was not found: $composeFile" }
  $result = Invoke-DockerBounded -Arguments @('compose', '-f', $composeFile, 'up', '-d') -TimeoutSeconds 20
  $detail = (@($result.output, $result.error) | Where-Object { $_ }) -join '; '
  if ($result.timed_out -or $result.exit_code -ne 0) {
    throw "Qdrant docker compose startup failed: $detail"
  }
  return $detail
}

function Invoke-SoftRecovery {
  $docker = Test-DockerReady
  if (-not $docker.ready) {
    Add-Step 'docker-desktop-start' 'attempted' $docker.detail
    $docker = Start-DockerDesktopBounded
  }
  if (-not $docker.ready) { throw "Docker engine did not become ready: $($docker.detail)" }
  Add-Step 'docker-engine' 'ready' $docker.detail
  Add-Step 'compose-up' 'running'
  $composeDetail = Start-QdrantCompose
  Add-Step 'compose-up' 'passed' $composeDetail
  $qdrant = Wait-QdrantReady
  if (-not $qdrant.ready) { throw "Qdrant did not become HTTP-ready: $($qdrant.detail)" }
  Add-Step 'qdrant-health' 'ready' $qdrant.detail
}

function Invoke-ForceRecovery {
  if ($WhatIf) {
    foreach ($name in @('stop-docker-service', 'terminate-docker-processes', 'wsl-shutdown', 'start-docker-service', 'start-docker-desktop', 'compose-up', 'qdrant-health')) {
      Add-Step $name 'whatif'
    }
    return
  }
  Add-Step 'stop-docker-service' 'running'
  Stop-Service -Name 'com.docker.service' -Force -ErrorAction SilentlyContinue
  Add-Step 'stop-docker-service' 'passed'
  Add-Step 'terminate-docker-processes' 'running'
  foreach ($name in @('Docker Desktop', 'com.docker.backend')) {
    Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  }
  Add-Step 'terminate-docker-processes' 'passed'
  Add-Step 'wsl-shutdown' 'running'
  $wsl = & wsl.exe --shutdown 2>&1
  if ($LASTEXITCODE -ne 0) { throw "wsl --shutdown failed: $wsl" }
  Add-Step 'wsl-shutdown' 'passed'
  Add-Step 'start-docker-service' 'running'
  Start-Service -Name 'com.docker.service' -ErrorAction SilentlyContinue
  Add-Step 'start-docker-service' 'passed'
  Add-Step 'start-docker-desktop' 'running'
  $docker = Start-DockerDesktopBounded
  if (-not $docker.ready) { throw "Docker engine did not recover: $($docker.detail)" }
  Add-Step 'start-docker-desktop' 'passed' $docker.detail
  Add-Step 'compose-up' 'running'
  $composeDetail = Start-QdrantCompose
  Add-Step 'compose-up' 'passed' $composeDetail
  $qdrant = Wait-QdrantReady
  if (-not $qdrant.ready) { throw "Qdrant did not become HTTP-ready after force recovery: $($qdrant.detail)" }
  Add-Step 'qdrant-health' 'ready' $qdrant.detail
}

try {
  $initialDocker = Test-DockerReady
  $initialQdrant = Test-QdrantReady
  Add-Step 'initial-probe' $(if ($initialDocker.ready -and $initialQdrant.ready) { 'ready' } else { 'degraded' }) "docker=$($initialDocker.detail); qdrant=$($initialQdrant.detail)"

  if ($initialDocker.ready -and $initialQdrant.ready) {
    $result = 'already-ready'
  } elseif ($WhatIf) {
    Add-Step 'soft-recovery' 'whatif'
    if ($Force) { Invoke-ForceRecovery } else { Add-Step 'force-recovery' 'skipped' 're-run with -Force for host reset' }
    $result = 'whatif'
  } else {
    try {
      Invoke-SoftRecovery
      $result = 'soft-recovered'
    } catch {
      Add-Step 'soft-recovery' 'failed' $_.Exception.Message
      if (-not $Force) { throw "Qdrant soft recovery failed. Re-run with -Force for the explicit Docker/WSL reset. $($_.Exception.Message)" }
      # Soft recovery owns the first bounded budget.  A slow Docker Desktop
      # probe must not consume the entire budget reserved for the explicitly
      # operator-approved force reset.
      $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
      Invoke-ForceRecovery
      $result = 'force-recovered'
    }
  }
} catch {
  Add-Step 'recovery' 'failed' $_.Exception.Message
  $result = 'failed'
  $errorMessage = $_.Exception.Message
}

New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
$receipt = [ordered]@{
  schema_version = 'bhm.qdrant.recovery.v1'
  result = $result
  force_requested = [bool]$Force
  whatif = [bool]$WhatIf
  health_url = $qdrantHealthUrl
  compose_file = $composeFile
  steps = @($steps)
  completed_at = [DateTime]::UtcNow.ToString('o')
}
if ($errorMessage) { $receipt.error = $errorMessage.Substring(0, [Math]::Min($errorMessage.Length, 1000)) }
$receipt | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $receiptPath -Encoding UTF8
$receipt | ConvertTo-Json -Depth 6
if ($result -eq 'failed') { exit 1 }
