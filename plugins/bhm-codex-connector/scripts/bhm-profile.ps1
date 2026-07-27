param(
    [ValidateSet("status", "low-context", "standard", "deep", "compare")]
    [string]$Action = "status",
    [string]$EnvPath = "C:\Users\xman\.bhm\.env",
    [switch]$RestartWorker,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$setProfileScript = Join-Path $scriptRoot "set-bhm-profile.ps1"
$compareScript = Join-Path $scriptRoot "compare-bhm-profiles.ps1"
$profilesPath = Join-Path (Split-Path -Parent $scriptRoot) "profiles\bhm-profiles.json"

function Get-EnvMap {
    param([string]$Path)

    $map = [ordered]@{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $map
    }

    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line.TrimStart().StartsWith("#")) { continue }
        $parts = $line -split "=", 2
        if ($parts.Count -ne 2) { continue }
        $map[$parts[0].Trim()] = $parts[1].Trim()
    }
    return $map
}

function Emit-Result {
    param([object]$Value)

    if ($AsJson) {
        $Value | ConvertTo-Json -Depth 20
    }
    else {
        $Value
    }
}

switch ($Action) {
    "status" {
        $envMap = Get-EnvMap -Path $EnvPath
        $profiles = Get-Content -Raw -LiteralPath $profilesPath -Encoding UTF8 | ConvertFrom-Json
        $legacyKeys = @($profiles.legacy_aliases.PSObject.Properties.Name)
        $legacyPresent = @($legacyKeys | Where-Object { $envMap.Contains($_) })
        $result = [ordered]@{
            ok = $true
            action = "status"
            env_path = $EnvPath
            namespace = [string]$profiles.namespace
            schema_version = [int]$profiles.schema_version
            current = [ordered]@{
                context_profile = $envMap["BHM_CONTEXT_PROFILE"]
                context_token_budget = $envMap["BHM_CONTEXT_TOKEN_BUDGET"]
                observation_max_per_session = $envMap["BHM_OBSERVATION_MAX_PER_SESSION"]
                context_summarize_chunk_size = $envMap["BHM_CONTEXT_SUMMARIZE_CHUNK_SIZE"]
                context_summarize_concurrency = $envMap["BHM_CONTEXT_SUMMARIZE_CONCURRENCY"]
                graph_extraction_batch_size = $envMap["BHM_GRAPH_EXTRACTION_BATCH_SIZE"]
                llm_timeout_ms = $envMap["BHM_LLM_TIMEOUT_MS"]
                retrieval_bm25_weight = $envMap["BHM_RETRIEVAL_BM25_WEIGHT"]
                retrieval_vector_weight = $envMap["BHM_RETRIEVAL_VECTOR_WEIGHT"]
                retrieval_graph_weight = $envMap["BHM_RETRIEVAL_GRAPH_WEIGHT"]
            }
            legacy_keys_present = $legacyPresent
            legacy_migration_required = ($legacyPresent.Count -gt 0)
            recommended = "low-context"
            note = "Profiles use BHM-native BHM_* keys; applying a profile removes known legacy AgentMemory aliases after creating a backup."
        }
        Emit-Result -Value $result
        break
    }

    "low-context" {
        $args = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $setProfileScript,
            "-Profile", "low-context",
            "-AsJson"
        )
        if ($RestartWorker) {
            $args += "-RestartWorker"
        }
        $json = & powershell @args
        $result = $json | ConvertFrom-Json
        Emit-Result -Value $result
        break
    }

    "standard" {
        $args = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $setProfileScript,
            "-Profile", "standard",
            "-AsJson"
        )
        if ($RestartWorker) {
            $args += "-RestartWorker"
        }
        $json = & powershell @args
        $result = $json | ConvertFrom-Json
        Emit-Result -Value $result
        break
    }

    "deep" {
        $args = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $setProfileScript,
            "-Profile", "deep",
            "-AsJson"
        )
        if ($RestartWorker) {
            $args += "-RestartWorker"
        }
        $json = & powershell @args
        $result = $json | ConvertFrom-Json
        Emit-Result -Value $result
        break
    }

    "compare" {
        $args = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $compareScript,
            "-AsJson"
        )
        $json = & powershell @args
        $result = $json | ConvertFrom-Json
        Emit-Result -Value $result
        break
    }
}
