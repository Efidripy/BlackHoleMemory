param(
  [ValidateSet('Start', 'Stop', 'Status')][string]$Action = 'Start',
  [ValidateRange(1, 300)][int]$PollSeconds = 5,
  [ValidateRange(1, 1000)][int]$BatchSize = 10,
  [ValidateRange(1, 100)][int]$MaxAttempts = 5,
  [string]$OpenAiBaseUrl = '',
  [switch]$NoWait
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$bootstrapRoot = Join-Path $repoRoot '.runtime\bootstrap'
$pidPath = Join-Path $bootstrapRoot 'projection-sidecar.pid'
$stopPath = Join-Path $bootstrapRoot 'projection-sidecar.stop'
$runner = Join-Path $repoRoot 'scripts\run-bhm-projection-sidecar.ps1'
$stdoutPath = Join-Path $bootstrapRoot 'projection-sidecar.launcher.stdout.log'
$stderrPath = Join-Path $bootstrapRoot 'projection-sidecar.launcher.stderr.log'

function Get-SidecarProcess {
  try {
    return @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
      $_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'run-bhm-projection-sidecar\.ps1'
    })
  } catch {
    return @()
  }
}

function Get-SidecarStatus {
  $processes = @(Get-SidecarProcess)
  $pidProcess = Get-PidFileProcess
  $pids = @($processes | ForEach-Object { [int]$_.ProcessId })
  if ($null -ne $pidProcess -and $pids -notcontains [int]$pidProcess.Id) {
    $pids += [int]$pidProcess.Id
  }
  [pscustomobject]@{
    running = $processes.Count -gt 0 -or $null -ne $pidProcess
    pids = @($pids | Sort-Object -Unique)
    pid_file = $pidPath
    stdout = $stdoutPath
    stderr = $stderrPath
  }
}

function Get-PidFileProcess {
  if (-not (Test-Path -LiteralPath $pidPath)) { return $null }
  try {
    $rawPid = (Get-Content -LiteralPath $pidPath -Raw -ErrorAction Stop).Trim()
    $pidValue = 0
    if (-not [int]::TryParse($rawPid, [ref]$pidValue) -or $pidValue -le 0) { return $null }
    return Get-Process -Id $pidValue -ErrorAction Stop
  } catch {
    return $null
  }
}

New-Item -ItemType Directory -Force -Path $bootstrapRoot | Out-Null

if ($Action -eq 'Status') {
  Get-SidecarStatus | ConvertTo-Json -Depth 5
  exit 0
}

if ($Action -eq 'Stop') {
  New-Item -ItemType File -Force -Path $stopPath | Out-Null
  $pidProcess = Get-PidFileProcess
  if ($null -ne $pidProcess) {
    Stop-Process -Id $pidProcess.Id -Force -ErrorAction SilentlyContinue
  }
  foreach ($process in @(Get-SidecarProcess)) {
    Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
  }
  Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
  [pscustomobject]@{ ok = $true; action = 'stopped' } | ConvertTo-Json -Depth 4
  exit 0
}

if (-not (Test-Path -LiteralPath $runner)) {
  throw "Missing projection sidecar runner: $runner"
}
$pidProcess = Get-PidFileProcess
if ($null -ne $pidProcess) {
  [pscustomobject]@{ ok = $true; action = 'already-running'; pid = [int]$pidProcess.Id; status = Get-SidecarStatus } | ConvertTo-Json -Depth 6
  exit 0
}
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
if (@(Get-SidecarProcess).Count -gt 0) {
  [pscustomobject]@{ ok = $true; action = 'already-running'; status = Get-SidecarStatus } | ConvertTo-Json -Depth 6
  exit 0
}
Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue
$lmStudioUrl = ''
if ([string]::IsNullOrWhiteSpace($OpenAiBaseUrl)) {
  . (Join-Path $repoRoot 'scripts\runtime-endpoints.ps1')
  $lmStudioUrl = Get-BhmRuntimeEndpoint -Name 'lm_studio' -RepoRoot $repoRoot
} else {
  $lmStudioUrl = $OpenAiBaseUrl
}
$providerUri = $null
if (-not [Uri]::TryCreate($lmStudioUrl, [UriKind]::Absolute, [ref]$providerUri) -or
    $providerUri.Scheme -ne 'http' -or
    @('127.0.0.1', 'localhost', '::1') -notcontains $providerUri.Host.ToLowerInvariant() -or
    -not [string]::IsNullOrWhiteSpace($providerUri.UserInfo) -or
    -not [string]::IsNullOrWhiteSpace($providerUri.Query) -or
    -not [string]::IsNullOrWhiteSpace($providerUri.Fragment)) {
  throw 'Projection sidecar requires a credential-free HTTP loopback provider URL.'
}
$lmStudioUrl = $providerUri.AbsoluteUri.TrimEnd('/')
$arguments = @(
  '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $runner,
  '-PollSeconds', [string]$PollSeconds,
  '-BatchSize', [string]$BatchSize,
  '-MaxAttempts', [string]$MaxAttempts,
  '-OpenAiBaseUrl', $lmStudioUrl
)
$process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments `
  -WorkingDirectory $repoRoot -WindowStyle Hidden `
  -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
if (-not $NoWait) { Start-Sleep -Seconds 2 }
[pscustomobject]@{ ok = $true; action = 'started'; pid = [int]$process.Id; status = Get-SidecarStatus } | ConvertTo-Json -Depth 6
