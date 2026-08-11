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
  while (-not (Test-Path -LiteralPath $stopPath)) {
    $arguments = @(
      (Join-Path $repoRoot 'scripts\run-bhm-projection-worker.py'),
      '--loop',
      '--force',
      '--max-cycles', '1'
    )
    if (-not [string]::IsNullOrWhiteSpace($OpenAiBaseUrl)) {
      $arguments += @('--openai-base-url', $OpenAiBaseUrl)
    }
    try {
      & $pythonPath @arguments >> $stdoutPath 2>> $stderrPath
    } catch {
      Add-Content -LiteralPath $stderrPath -Value ("sidecar invocation failed: " + $_.Exception.Message)
    }
    if (Test-Path -LiteralPath $stopPath) { break }
    Start-Sleep -Seconds $PollSeconds
  }
}
finally {
  Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue
}
