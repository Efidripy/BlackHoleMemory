param(
    [ValidateRange(0, 65535)][int]$Port = 0,
    [switch]$Open,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

$pluginRoot = Split-Path -Parent $PSScriptRoot
$runtimeConfigPath = Join-Path $pluginRoot "config\runtime-discovery.json"
if ($Port -le 0 -and (Test-Path -LiteralPath $runtimeConfigPath)) {
    $runtime = Get-Content -Raw -LiteralPath $runtimeConfigPath | ConvertFrom-Json
    $candidate = @($runtime.workbenchCandidates) | Where-Object { $_ } | Select-Object -First 1
    if ($candidate) { $Port = ([Uri]$candidate).Port }
}
if ($Port -le 0 -and $env:BHM_WORKBENCH_PORT) { $Port = [int]$env:BHM_WORKBENCH_PORT }
if ($Port -le 0) { throw 'BHM_WORKBENCH_PORT or runtime-discovery.json workbenchCandidates is required' }
$serverScript = Join-Path $PSScriptRoot "bhm-workbench-server.mjs"
$runtimeStateRoot = Join-Path $env:USERPROFILE ".codex\plugin-data\bhm\runtime"
$runtimeLogRoot = Join-Path $runtimeStateRoot "logs"
$capabilityPath = Join-Path $runtimeStateRoot "workbench-capability.txt"
$stdoutLog = Join-Path $runtimeLogRoot "bhm-workbench-stdout.log"
$stderrLog = Join-Path $runtimeLogRoot "bhm-workbench-stderr.log"

New-Item -ItemType Directory -Force -Path $runtimeLogRoot | Out-Null

function New-BhmWorkbenchCapability {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) }
    finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Initialize-BhmWorkbenchCallerCredential {
    $token = [string][Environment]::GetEnvironmentVariable('BHM_CALLER_TOKEN', 'User')
    if ($token.Length -lt 32) { $token = [string]$env:BHM_CALLER_TOKEN }
    if ($token.Length -lt 32) {
        $token = New-BhmWorkbenchCapability
        [Environment]::SetEnvironmentVariable('BHM_CALLER_TOKEN', $token, 'User')
        [Environment]::SetEnvironmentVariable('BHM_CALLER_ID', 'local-operator', 'User')
        [Environment]::SetEnvironmentVariable('BHM_CALLER_PROJECTS', '*', 'User')
        [Environment]::SetEnvironmentVariable('BHM_CALLER_DEFAULT_PROJECT', 'blackholememory', 'User')
    }
    $env:BHM_CALLER_TOKEN = $token
}

function Get-BhmWorkbenchLaunchUrl([string]$Capability) {
    $encoded = [Uri]::EscapeDataString($Capability)
    return "http://127.0.0.1:$Port/#bhm-workbench-capability=$encoded"
}

Initialize-BhmWorkbenchCallerCredential

$existing = Get-CimInstance Win32_Process -Filter "name = 'node.exe'" |
    Where-Object { $_.CommandLine -like "*bhm-workbench-server.mjs*" }

if ($existing) {
    if (-not (Test-Path -LiteralPath $capabilityPath)) {
        throw "Existing BHM Workbench has no session capability; stop the legacy process and start it again"
    }
    $capability = (Get-Content -Raw -LiteralPath $capabilityPath).Trim()
    if ($capability.Length -lt 32) { throw "Stored BHM Workbench capability is invalid; restart is required" }
    if ($Open) { Start-Process (Get-BhmWorkbenchLaunchUrl -Capability $capability) | Out-Null }
    $result = [ordered]@{
        ok = $true
        status = "already-running"
        pid = @($existing | Select-Object -First 1 -ExpandProperty ProcessId)
        url = "http://127.0.0.1:$Port"
        opened = [bool]$Open
        stdout_log = $stdoutLog
        stderr_log = $stderrLog
    }
    if ($AsJson) { $result | ConvertTo-Json -Depth 10; exit 0 }
    $result
    exit 0
}

$capability = New-BhmWorkbenchCapability
[System.IO.File]::WriteAllText($capabilityPath, $capability, [System.Text.Encoding]::ASCII)
$previousPort = $env:BHM_WORKBENCH_PORT
$previousCapability = $env:BHM_WORKBENCH_CAPABILITY
$env:BHM_WORKBENCH_PORT = [string]$Port
$env:BHM_WORKBENCH_CAPABILITY = $capability
try {
    $proc = Start-Process node `
        -ArgumentList $serverScript `
        -WorkingDirectory $pluginRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
}
finally {
    if ($null -eq $previousPort) {
        Remove-Item Env:BHM_WORKBENCH_PORT -ErrorAction SilentlyContinue
    }
    else {
        $env:BHM_WORKBENCH_PORT = $previousPort
    }
    if ($null -eq $previousCapability) {
        Remove-Item Env:BHM_WORKBENCH_CAPABILITY -ErrorAction SilentlyContinue
    }
    else {
        $env:BHM_WORKBENCH_CAPABILITY = $previousCapability
    }
}

Start-Sleep -Seconds 2
if ($proc.HasExited) {
    Remove-Item -LiteralPath $capabilityPath -Force -ErrorAction SilentlyContinue
    throw "BHM Workbench exited during startup; inspect $stderrLog"
}
if ($Open) { Start-Process (Get-BhmWorkbenchLaunchUrl -Capability $capability) | Out-Null }

$result = [ordered]@{
    ok = $true
    status = "started"
    pid = $proc.Id
    url = "http://127.0.0.1:$Port"
    opened = [bool]$Open
    stdout_log = $stdoutLog
    stderr_log = $stderrLog
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 10
    exit 0
}

$result
