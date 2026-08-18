param(
  [ValidateRange(1, 300)][int]$PollSeconds = 5,
  [ValidateRange(1, 1000)][int]$BatchSize = 10,
  [ValidateRange(1, 100)][int]$MaxAttempts = 5,
  [string]$OpenAiBaseUrl = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $repoRoot '.runtime'
$bootstrapRoot = Join-Path $runtimeRoot 'bootstrap'
$pidPath = Join-Path $bootstrapRoot 'projection-sidecar.pid'
$stopPath = Join-Path $bootstrapRoot 'projection-sidecar.stop'
$stdoutPath = Join-Path $bootstrapRoot 'projection-sidecar.stdout.log'
$stderrPath = Join-Path $bootstrapRoot 'projection-sidecar.stderr.log'
$pythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
  $pythonPath = (Get-Command python -ErrorAction Stop).Source
}

New-Item -ItemType Directory -Force -Path $bootstrapRoot | Out-Null
Set-Content -LiteralPath $pidPath -Value ([string]$PID) -Encoding UTF8

# The sidecar is deliberately not the BHM service. It consumes only the
# transactional outbox and projects to Qdrant; SQLite remains authoritative.
$env:BHM_MEMORY_STORE_MODE = 'sqlite-shadow'
$env:BHM_PROJECTION_WORKER_ENABLED = 'true'
$env:BHM_MEMORY_STORE_PARITY_CONFIRMED = 'false'
$env:BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED = 'true'
$env:BHM_PROJECTION_WORKER_BATCH_SIZE = [string]$BatchSize
$env:BHM_PROJECTION_WORKER_MAX_ATTEMPTS = [string]$MaxAttempts
$env:BHM_FALLBACK_MODE = 'explicit'

try {
  $failureStreak = 0
  while (-not (Test-Path -LiteralPath $stopPath)) {
    $arguments = @(
      (Join-Path $repoRoot 'scripts\run-bhm-projection-worker.py'),
      '--loop',
      '--force',
      '--max-cycles', '1',
      '--quiet-idle'
    )
    if (-not [string]::IsNullOrWhiteSpace($OpenAiBaseUrl)) {
      $arguments += @('--openai-base-url', $OpenAiBaseUrl)
    }
    try {
      # Native stderr is expected for the worker's structured infrastructure
      # diagnostic. Do not let PowerShell convert it into a one-line
      # NativeCommandError that discards the bounded JSON payload.
      $savedErrorActionPreference = $ErrorActionPreference
      try {
        $ErrorActionPreference = 'Continue'
        & $pythonPath @arguments >> $stdoutPath 2>> $stderrPath
        $workerExitCode = $LASTEXITCODE
      } finally {
        $ErrorActionPreference = $savedErrorActionPreference
      }
    } catch {
      $workerExitCode = 1
      $boundedError = [string]$_.Exception.Message
      if ($boundedError.Length -gt 2000) { $boundedError = $boundedError.Substring(0, 2000) }
      Add-Content -LiteralPath $stderrPath -Value (([pscustomobject]@{
        timestamp = [DateTimeOffset]::UtcNow.ToString('o')
        classification = 'sidecar_invocation_failed'
        error = $boundedError
      }) | ConvertTo-Json -Compress)
    }
    if (Test-Path -LiteralPath $stopPath) { break }
    if ($workerExitCode -eq 75) {
      $failureStreak += 1
      $exponent = [Math]::Min([Math]::Max($failureStreak - 1, 0), 8)
      $delaySeconds = [Math]::Min(300, [int]($PollSeconds * [Math]::Pow(2, $exponent)))
    } else {
      if ($failureStreak -gt 0 -and $workerExitCode -eq 0) {
        Add-Content -LiteralPath $stdoutPath -Value (([pscustomobject]@{
          timestamp = [DateTimeOffset]::UtcNow.ToString('o')
          classification = 'infrastructure_recovered'
        }) | ConvertTo-Json -Compress)
      }
      $failureStreak = 0
      $delaySeconds = $PollSeconds
      if ($workerExitCode -ne 0) {
        Add-Content -LiteralPath $stderrPath -Value (([pscustomobject]@{
          timestamp = [DateTimeOffset]::UtcNow.ToString('o')
          classification = 'worker_exit'
          exit_code = [int]$workerExitCode
        }) | ConvertTo-Json -Compress)
      }
    }
    Start-Sleep -Seconds $delaySeconds
  }
}
finally {
  Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue
}
