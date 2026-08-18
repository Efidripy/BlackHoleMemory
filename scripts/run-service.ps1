param(
  [switch]$SkipInstall,
  [string]$ProjectRoot = '',
  [switch]$Authoritative,
  [switch]$SemanticFusion
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
  Split-Path -Parent $PSScriptRoot
} else {
  (Resolve-Path -LiteralPath $ProjectRoot).Path
}
$repoRoot = [System.IO.Path]::GetFullPath($repoRoot)
$srcPath = Join-Path $repoRoot "src"
. (Join-Path $repoRoot "scripts\runtime-endpoints.ps1")
. (Join-Path $repoRoot "scripts\bhm-caller-credential.ps1")
$callerCredential = Initialize-BhmCallerCredential
Write-Host "[INFO] BHM caller credential ready: source=$($callerCredential.source) fingerprint=$($callerCredential.fingerprint)"
# The destructive/operator capability is deliberately separate from the
# caller token. Load it from the Windows User environment only when the
# launcher did not already provide a process-scoped value; never print it.
if ([string]::IsNullOrWhiteSpace([string]$env:BHM_ADMIN_CAPABILITY)) {
  $userAdminCapability = [string][Environment]::GetEnvironmentVariable('BHM_ADMIN_CAPABILITY', 'User')
  if ($userAdminCapability.Length -ge 32) {
    $env:BHM_ADMIN_CAPABILITY = $userAdminCapability
  }
}
$apiParts = Get-BhmRuntimeEndpointParts -Name "bhm_api" -RepoRoot $repoRoot
$lmStudioUrl = Get-BhmRuntimeEndpoint -Name "lm_studio" -RepoRoot $repoRoot
$lmStudioPort = (Get-BhmRuntimeEndpointParts -Name "lm_studio" -RepoRoot $repoRoot).Port
$env:BHM_HOST = if ($env:BHM_HOST) { $env:BHM_HOST } else { $apiParts.Host }
$env:BHM_PORT = if ($env:BHM_PORT) { $env:BHM_PORT } else { [string]$apiParts.Port }
Assert-BhmApiLoopbackHost -HostName ([string]$env:BHM_HOST)

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

function Resolve-AuthoritativeProviderEndpoint {
  $configured = Get-ConfiguredOpenAiBaseUrl
  $loopback = $lmStudioUrl
  if ($configured -match ('^https?://172\.18\.0\.1:' + $lmStudioPort + '/v1/?$') -and
      (Test-OpenAiBaseUrl -BaseUrl $loopback)) {
    $env:OPENAI_BASE_URL = $loopback
    # Pydantic Settings also accepts the field-scoped BHM override. Keep both
    # process variables aligned so a stale Docker-host value cannot survive
    # into Mem0's embedding client when the authoritative service runs on
    # Windows.
    $env:BHM_MEM0_OPENAI_BASE_URL = $loopback
  }
}

if ($Authoritative) {
  $env:BHM_MEMORY_STORE_MODE = "sqlite-authoritative"
  $env:BHM_QDRANT_REQUIRED_FOR_CORE = "false"
  $env:BHM_FALLBACK_MODE = "explicit"
  $env:BHM_PROJECTION_WORKER_ENABLED = "false"
  $env:BHM_MEMORY_STORE_PARITY_CONFIRMED = "true"
  $env:BHM_MEMORY_STORE_WRITER_OFFLINE_CONFIRMED = "true"
  Resolve-AuthoritativeProviderEndpoint
}

# Semantic fusion is never implicit.  The authoritative launcher may pass
# this explicit operator switch when a controlled metadata-only probe is
# intended; otherwise the child process stays lexical-only.
if ($SemanticFusion) {
  $env:BHM_CODE_SEMANTIC_FUSION = "1"
  # Provider-backed semantic search is explicit and fail-closed: require a
  # current SQLite graph epoch, complete code-metadata projection and a
  # pre-warmed provider before any semantic query can call the provider.
  $env:BHM_SEMANTIC_READINESS_GATE = "1"
  # Explicit semantic mode also preloads the embedding model.  This remains
  # opt-in and read-only; lexical/default startup does not contact embeddings.
  $env:BHM_PROVIDER_EMBEDDING_WARMUP = "1"
  $env:BHM_PROVIDER_MEMORY_WARMUP = "1"
} else {
  # Do not inherit a stale semantic operator gate from a previous shell.
  $env:BHM_SEMANTIC_READINESS_GATE = "0"
}

if (-not (Test-Path "$repoRoot\.venv")) {
  python -m venv "$repoRoot\.venv"
}

if (-not $SkipInstall) {
  & "$repoRoot\.venv\Scripts\python.exe" -m pip install --upgrade pip
  & "$repoRoot\.venv\Scripts\python.exe" -m pip install -e $repoRoot
}

if ($env:PYTHONPATH) {
  $env:PYTHONPATH = "$srcPath;$env:PYTHONPATH"
} else {
  $env:PYTHONPATH = $srcPath
}

& "$repoRoot\.venv\Scripts\python.exe" -m uvicorn blackholememory.app:app --host $apiParts.Host --port $apiParts.Port
