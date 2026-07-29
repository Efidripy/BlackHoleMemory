param(
  [switch]$NoWait,
  [switch]$ProbeOnly,
  [switch]$ForceRestart,
  [switch]$SemanticFusion,
  [string]$BaseUrl = '',
  [ValidateRange(5, 300)][int]$TimeoutSec = 90,
  [ValidateRange(1, 10)][int]$PollSeconds = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
$apiParts = Get-BhmRuntimeEndpointParts -Name 'bhm_api' -RepoRoot $repoRoot
$lmStudioParts = Get-BhmRuntimeEndpointParts -Name 'lm_studio' -RepoRoot $repoRoot
$lmStudioUrl = Get-BhmRuntimeEndpoint -Name 'lm_studio' -RepoRoot $repoRoot
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot }
$env:BHM_HOST = if ($env:BHM_HOST) { $env:BHM_HOST } else { $apiParts.Host }
$env:BHM_PORT = if ($env:BHM_PORT) { $env:BHM_PORT } else { [string]$apiParts.Port }

function Set-AuthoritativeEnvironment {
  $env:BHM_MEMORY_STORE_MODE = "sqlite-authoritative"
  $env:BHM_FALLBACK_MODE = "explicit"
  $env:BHM_PROJECTION_WORKER_ENABLED = "false"
  $env:BHM_MEMORY_STORE_PARITY_CONFIRMED = "true"
  $env:BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED = "true"
  Resolve-LocalLmStudioEndpoint
}

function Get-ConfiguredOpenAiBaseUrl {
  if ($env:OPENAI_BASE_URL) {
    return $env:OPENAI_BASE_URL.Trim().TrimEnd('/')
  }

  $envPath = Join-Path $HOME '.bhm\.env'
  if (-not (Test-Path -LiteralPath $envPath)) {
    return ''
  }

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
  param([Parameter(Mandatory = $true)][string]$BaseUrl)

  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri "$($BaseUrl.TrimEnd('/'))/models" -TimeoutSec 2
    return [int]$response.StatusCode -eq 200
  } catch {
    return $false
  }
}

function Resolve-LocalLmStudioEndpoint {
  $configured = Get-ConfiguredOpenAiBaseUrl
  $loopback = $lmStudioUrl

  # The shared ~/.bhm/.env may retain the Docker-host address from a prior
  # container layout. BHM's authoritative service is a Windows process, so
  # prefer LM Studio's loopback only when that exact stale local value is in
  # use and the loopback API is live. Explicit remote/custom endpoints remain
  # untouched.
  if ($configured -match ('^https?://172\.18\.0\.1:' + $lmStudioParts.Port + '/v1/?$') -and
      (Test-OpenAiBaseUrl -BaseUrl $loopback)) {
    $env:OPENAI_BASE_URL = $loopback
    $env:BHM_MEM0_OPENAI_BASE_URL = $loopback
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
    # A normal operator token may not have CIM process-inspection rights.
    # Listener ownership remains the authoritative bounded fallback; do not
    # turn a permissions limitation into a launcher failure.
    @()
  }
}

function Start-BhmDetachedHidden {
  param(
    [Parameter(Mandatory = $true)][string]$FilePath,
    [Parameter(Mandatory = $true)][string[]]$ArgumentList,
    [Parameter(Mandatory = $true)][string]$WorkingDirectory,
    [Parameter(Mandatory = $true)][string]$StdoutPath,
    [Parameter(Mandatory = $true)][string]$StderrPath
  )

  # PowerShell Start-Process can fail before launch when the inherited Windows
  # environment contains both Path and PATH. ShellExecute delegates the
  # environment handoff to Windows and avoids that case without copying or
  # logging credentials.
  $psi = [System.Diagnostics.ProcessStartInfo]::new()
  $psi.FileName = $FilePath
  $psi.Arguments = (($ArgumentList | ForEach-Object {
      if ($_ -match '[\s"]') { '"' + $_.Replace('"', '\\"') + '"' } else { $_ }
    }) -join ' ')
  $psi.WorkingDirectory = $WorkingDirectory
  $psi.UseShellExecute = $true
  $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
  $psi.Verb = ''
  [System.Diagnostics.Process]::Start($psi) | Out-Null
}

function Get-BhmListeningProcessIds {
  $ids = @()
  $matches = netstat -ano | Select-String ([regex]::Escape("$($apiParts.Host):$($apiParts.Port)"))
  foreach ($line in $matches) {
    $parts = ($line.ToString() -split '\s+') | Where-Object { $_ }
    if ($parts.Length -lt 5 -or $parts[0] -ne 'TCP' -or
        $parts[1] -ne "$($apiParts.Host):$($apiParts.Port)" -or $parts[3] -ne 'LISTENING') {
      continue
    }
    $listenerPid = 0
    if ([int]::TryParse($parts[4], [ref]$listenerPid) -and $listenerPid -gt 0) {
      $ids += $listenerPid
    }
  }
  @($ids | Sort-Object -Unique)
}

function Stop-BhmProcesses {
  $processes = @(Get-BhmProcesses)
  $knownIds = @($processes | ForEach-Object { [int]$_.ProcessId })
  foreach ($proc in $processes) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
  }
  foreach ($listenerId in @(Get-BhmListeningProcessIds)) {
    if ($knownIds -contains [int]$listenerId) {
      Stop-Process -Id $listenerId -Force -ErrorAction SilentlyContinue
    }
  }
}

function Get-ContractSnapshot {
  param([string]$BaseUrl = '')

  if ([string]::IsNullOrWhiteSpace($BaseUrl)) { $BaseUrl = Get-BhmRuntimeEndpoint -Name 'bhm_api' -RepoRoot $repoRoot }

  try {
    # The health/cutover surfaces can legitimately take a few seconds on a
    # cold Windows/Qdrant start. Keep the probe bounded, but do not turn a
    # healthy slow response into a false launcher failure.
    $health = Invoke-RestMethod -UseBasicParsing -Uri "$BaseUrl/bhm/health" -TimeoutSec 10
    $cutover = Invoke-RestMethod -UseBasicParsing -Uri "$BaseUrl/health/cutover" -TimeoutSec 10
    return [pscustomobject]@{
      reachable = $true
      authoritative = (
        $health.status -eq 'healthy' -and
        $health.memory_store.backend -eq 'sqlite-authoritative' -and
        [bool]$health.memory_store.ready -and
        [bool]$health.memory_store.parity_confirmed -and
        [bool]$health.memory_store.writer_offline_confirmed -and
        [bool]$cutover.ok -and
        $cutover.mem0.status -eq 'projection-only' -and
        -not [bool]$cutover.mem0.direct_vector_writes -and
        -not [bool]$health.memory_store.projection_worker.enabled
      )
      health = $health
      cutover = $cutover
      error = ''
    }
  } catch {
    return [pscustomobject]@{
      reachable = $false
      authoritative = $false
      health = $null
      cutover = $null
      error = $_.Exception.Message
    }
  }
}

function Wait-Authoritative {
  param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
    [Parameter(Mandatory = $true)][int]$PollIntervalSeconds
  )

  $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
  $last = $null
  while ([DateTime]::UtcNow -lt $deadline) {
    $ready = $false
    try {
      $readyResponse = Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/health/ready" -TimeoutSec 3
      $ready = [int]$readyResponse.StatusCode -eq 200
    } catch {
      $ready = $false
    }
    $last = Get-ContractSnapshot -BaseUrl $BaseUrl
    if ($ready -and $last.authoritative) {
      return [pscustomobject]@{ ok = $true; ready = $true; snapshot = $last; error = '' }
    }
    Start-Sleep -Seconds $PollIntervalSeconds
  }
  [pscustomobject]@{
    ok = $false
    ready = $false
    snapshot = $last
    error = if ($null -ne $last -and $last.error) { $last.error } else { 'authoritative readiness timeout' }
  }
}

Set-AuthoritativeEnvironment
$initial = Get-ContractSnapshot -BaseUrl $BaseUrl
if ($ProbeOnly) {
  [pscustomobject]@{
    ok = [bool]$initial.authoritative
    action = 'probe-only'
    mode = 'sqlite-authoritative'
    reachable = [bool]$initial.reachable
    ready = [bool]$initial.authoritative
    error = $initial.error
  } | ConvertTo-Json -Depth 4
  if ($initial.authoritative) { exit 0 }
  exit 1
}
if ($ForceRestart -and $initial.reachable) {
  Stop-BhmProcesses
  Start-Sleep -Seconds 2
  $initial = Get-ContractSnapshot -BaseUrl $BaseUrl
}
if ($initial.authoritative) {
  [pscustomobject]@{
    ok = $true
    action = 'already-authoritative'
    mode = 'sqlite-authoritative'
    ready = $true
    worker_enabled = $false
  } | ConvertTo-Json -Depth 4
  exit 0
}

if (@(Get-BhmProcesses).Count -gt 0) {
  Stop-BhmProcesses
  Start-Sleep -Seconds 2
}

$serviceScript = Join-Path $repoRoot 'scripts\run-service.ps1'
$qdrantScript = Join-Path $repoRoot 'scripts\start-qdrant.ps1'
$runtimeInitializer = Join-Path $repoRoot 'scripts\initialize-bhm-runtime.py'
foreach ($requiredScript in @($serviceScript, $qdrantScript)) {
  if (-not (Test-Path -LiteralPath $requiredScript)) {
    throw "Missing startup script: $requiredScript"
  }
}
if (-not (Test-Path -LiteralPath $runtimeInitializer)) {
  throw "Missing runtime initializer: $runtimeInitializer"
}

$pythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
  $pythonCommand = Get-Command python -ErrorAction Stop
  $pythonPath = $pythonCommand.Source
}
$runtimeRoot = Join-Path $repoRoot '.runtime'
$initializerOutput = @(& $pythonPath $runtimeInitializer --runtime-dir $runtimeRoot 2>&1)
if ($LASTEXITCODE -ne 0) {
  throw "Authoritative runtime initialization failed: $($initializerOutput -join [Environment]::NewLine)"
}

$runtimeRoot = Join-Path $repoRoot '.runtime\bootstrap'
$serviceStdout = Join-Path $runtimeRoot 'authoritative-service-stdout.log'
$serviceStderr = Join-Path $runtimeRoot 'authoritative-service-stderr.log'
$qdrantStdout = Join-Path $runtimeRoot 'authoritative-qdrant-stdout.log'
$qdrantStderr = Join-Path $runtimeRoot 'authoritative-qdrant-stderr.log'
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
Start-BhmDetachedHidden -FilePath 'powershell.exe' `
  -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $qdrantScript) `
  -WorkingDirectory $repoRoot -StdoutPath $qdrantStdout -StderrPath $qdrantStderr
Start-BhmDetachedHidden -FilePath 'powershell.exe' `
  -ArgumentList (@('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $serviceScript, '-SkipInstall', '-Authoritative') + $(if ($SemanticFusion) { @('-SemanticFusion') } else { @() })) `
  -WorkingDirectory $repoRoot -StdoutPath $serviceStdout -StderrPath $serviceStderr
if ($NoWait) {
  [pscustomobject]@{
    ok = $true
    action = 'spawned'
    mode = 'sqlite-authoritative'
    ready = $false
    worker_enabled = $false
  } | ConvertTo-Json -Depth 4
  exit 0
}

$result = Wait-Authoritative -BaseUrl $BaseUrl -TimeoutSeconds $TimeoutSec -PollIntervalSeconds $PollSeconds
if (-not $result.ok) {
  Stop-BhmProcesses
  [pscustomobject]@{
    ok = $false
    action = 'rolled-back'
    mode = 'sqlite-authoritative'
    ready = $false
    error = $result.error
  } | ConvertTo-Json -Depth 4
  exit 1
}

[pscustomobject]@{
  ok = $true
  action = 'started-authoritative'
  mode = 'sqlite-authoritative'
  ready = $true
  worker_enabled = $false
  cutover = $result.snapshot.cutover.mem0.status
} | ConvertTo-Json -Depth 4
exit 0
