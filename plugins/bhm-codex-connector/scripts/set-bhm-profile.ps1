param(
    [ValidateSet("standard", "low-context", "deep")]
    [string]$Profile = "standard",
    [string]$EnvPath = "C:\Users\xman\.bhm\.env",
    [switch]$RestartWorker,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pluginRoot = Split-Path -Parent $scriptRoot
$profilesPath = Join-Path $pluginRoot "profiles\bhm-profiles.json"
$doctorScript = Join-Path $scriptRoot "bhm-doctor-activate.ps1"

if (-not (Test-Path -LiteralPath $EnvPath)) {
    throw "BHM env file not found: $EnvPath"
}

if (-not (Test-Path -LiteralPath $profilesPath)) {
    throw "Profiles file not found: $profilesPath"
}

$profiles = Get-Content -Raw -LiteralPath $profilesPath -Encoding UTF8 | ConvertFrom-Json
$selected = $profiles.$Profile
if ($null -eq $selected) {
    throw "Profile not found: $Profile"
}

$legacyAliases = @{}
foreach ($property in @($profiles.legacy_aliases.PSObject.Properties)) {
    $legacyAliases[$property.Name] = [string]$property.Value
}
$selectedKeys = @($selected.PSObject.Properties.Name)

$backupRoot = Join-Path $env:USERPROFILE ".codex\plugin-data\bhm\runtime\logs\profile-backups"
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
$backupPath = Join-Path $backupRoot ("bhm.env.{0}.bak" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
Copy-Item -LiteralPath $EnvPath -Destination $backupPath -Force

$lines = [System.Collections.Generic.List[string]]::new()
$existingKeys = @{}
$legacyKeysRemoved = [System.Collections.Generic.List[string]]::new()
foreach ($line in (Get-Content -LiteralPath $EnvPath -Encoding UTF8)) {
    $trimmed = $line.Trim()
    if (-not [string]::IsNullOrWhiteSpace($trimmed) -and -not $trimmed.StartsWith("#")) {
        $parts = $trimmed -split "=", 2
        if ($parts.Count -eq 2) {
            $key = $parts[0].Trim()
            if ($legacyAliases.ContainsKey($key)) {
                if (-not $legacyKeysRemoved.Contains($key)) {
                    [void]$legacyKeysRemoved.Add($key)
                }
                continue
            }
            if ($selectedKeys -contains $key) {
                if ($existingKeys.ContainsKey($key)) { continue }
                $existingKeys[$key] = $true
            }
        }
    }
    [void]$lines.Add($line)
}

foreach ($property in $selected.PSObject.Properties) {
    $key = $property.Name
    $value = [string]$property.Value
    $newLine = "{0}={1}" -f $key, $value
    $matched = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^\s*{0}\s*=" -f [regex]::Escape($key)) {
            $lines[$i] = $newLine
            $matched = $true
            break
        }
    }
    if (-not $matched) {
        [void]$lines.Add($newLine)
    }
}

$content = ($lines -join "`r`n") + "`r`n"
[System.IO.File]::WriteAllText($EnvPath, $content, [System.Text.UTF8Encoding]::new($false))

$restart = $null
if ($RestartWorker) {
    try {
        if (Test-Path -LiteralPath $doctorScript) {
            $doctorJson = & powershell -NoProfile -ExecutionPolicy Bypass -File $doctorScript -Project e-github-workspace -Title profile-recheck -Lightweight -AsJson 2>&1
            $doctorText = ($doctorJson | Out-String).Trim()
            if (-not [string]::IsNullOrWhiteSpace($doctorText)) {
                try {
                    $doctor = $doctorText | ConvertFrom-Json
                    $restart = [PSCustomObject]@{
                        status = if ($doctor.ok) { 'runtime-verified' } else { 'warning' }
                        final_verdict = $doctor.final_verdict
                        health_ok = $doctor.summary.health_ok
                        viewer_ok = $doctor.summary.viewer_ok
                    }
                } catch {
                    $restart = [PSCustomObject]@{
                        status = 'unknown'
                        raw = $doctorText
                    }
                }
            }
        }
        else {
            $restart = [PSCustomObject]@{
                status = 'manual-restart-required'
                note = 'Profile applied, but no plugin-local runtime verification script was found.'
            }
        }
    } catch {
        $restart = [PSCustomObject]@{
            status = 'failed'
            error = $_.Exception.Message
        }
    }
}

$result = [ordered]@{
    ok = $true
    profile = $Profile
    env_path = $EnvPath
    namespace = [string]$profiles.namespace
    schema_version = [int]$profiles.schema_version
    changed_keys = @($selected.PSObject.Properties.Name)
    legacy_keys_removed = @($legacyKeysRemoved)
    backup_path = $backupPath
    worker_restart = $restart
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 10
    exit 0
}

Write-Host "=== BHM Profile Applied ==="
Write-Host "- profile : $Profile"
Write-Host "- env     : $EnvPath"
Write-Host "- keys    : $(@($selected.PSObject.Properties.Name).Count)"
if ($RestartWorker -and $null -ne $restart) {
    Write-Host "- worker  : $($restart.health)"
}
