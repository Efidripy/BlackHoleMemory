param(
    [string]$ConfigPath = (Join-Path $env:USERPROFILE ".codex\config.toml"),
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
$canonicalPlugin = "bhm-codex-connector@bhm-local-marketplace"

function Convert-PluginSectionName {
    param([string]$RawName)

    $name = $RawName.Trim()
    if ($name.Length -ge 2 -and (($name.StartsWith("'") -and $name.EndsWith("'")) -or ($name.StartsWith('"') -and $name.EndsWith('"')))) {
        return $name.Substring(1, $name.Length - 2)
    }
    return $name
}

$enabledPlugins = @()
if (Test-Path -LiteralPath $ConfigPath) {
    $lines = Get-Content -LiteralPath $ConfigPath -Encoding UTF8
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -notmatch '^\s*\[plugins\.(.+)\]\s*$') { continue }
        $pluginName = Convert-PluginSectionName -RawName $Matches[1]
        $end = $index + 1
        while ($end -lt $lines.Count -and $lines[$end] -notmatch '^\s*\[') { $end++ }
        $body = if ($end -gt ($index + 1)) { $lines[($index + 1)..($end - 1)] } else { @() }
        if (($body -join "`n") -match '(?im)^\s*enabled\s*=\s*true\s*$') { $enabledPlugins += $pluginName }
    }
}

$enabledBhmConnectors = @($enabledPlugins | Where-Object { $_ -match '^bhm-codex-connector@' } | Sort-Object -Unique)
$canonicalEnabled = $enabledBhmConnectors -contains $canonicalPlugin
$duplicates = @($enabledBhmConnectors | Where-Object { $_ -ne $canonicalPlugin })
$duplicateActive = $duplicates.Count -gt 0

$result = [ordered]@{
    ok = (-not $duplicateActive)
    action = "bhm-plugin-duplicate-guard"
    config_path = $ConfigPath
    canonical_plugin = $canonicalPlugin
    canonical_enabled = $canonicalEnabled
    enabled_bhm_connectors = $enabledBhmConnectors
    duplicates = $duplicates
    action_required = $duplicateActive
    recommendation = if ($duplicateActive) {
        "BHM connector is already enabled through $canonicalPlugin. Do not enable another marketplace copy; remove the duplicate with: codex plugin remove <plugin@marketplace>."
    } elseif ($canonicalEnabled) {
        "Canonical BHM connector is already enabled. Do not install another marketplace copy."
    } else {
        "No enabled BHM connector was found. Enable only $canonicalPlugin."
    }
}

$result | ConvertTo-Json -Depth 10
