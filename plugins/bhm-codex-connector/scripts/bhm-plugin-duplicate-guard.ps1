param(
    [string]$ConfigPath = (Join-Path $env:USERPROFILE ".codex\config.toml"),
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
$personalPlugin = "bhm-codex-connector@bhm-marketplace"
$localMirrorPlugin = "bhm-codex-connector@bhm-local-marketplace"
$knownConnectorIdentities = @($personalPlugin, $localMirrorPlugin)

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
$personalEnabled = $enabledBhmConnectors -contains $personalPlugin
$localMirrorEnabled = $enabledBhmConnectors -contains $localMirrorPlugin
$coexistingIdentities = @($enabledBhmConnectors | Where-Object { $_ -in $knownConnectorIdentities })
$unrecognizedIdentities = @($enabledBhmConnectors | Where-Object { $_ -notin $knownConnectorIdentities })
$knownCoexistence = $personalEnabled -and $localMirrorEnabled

$result = [ordered]@{
    # The Personal marketplace identity can trigger Codex plugin/MCP discovery,
    # while the local identity remains the editable workspace mirror.  They ship
    # the same connector bundle and are allowed to coexist.  This guard is
    # advisory only: it must never turn a healthy Personal plugin into a false
    # duplicate or recommend an automatic removal.
    ok = ($unrecognizedIdentities.Count -eq 0)
    action = "bhm-plugin-duplicate-guard"
    config_path = $ConfigPath
    personal_plugin = $personalPlugin
    personal_enabled = $personalEnabled
    local_mirror_plugin = $localMirrorPlugin
    local_mirror_enabled = $localMirrorEnabled
    enabled_bhm_connectors = $enabledBhmConnectors
    coexisting_identities = $coexistingIdentities
    known_coexistence = $knownCoexistence
    # Retained for callers that consume the old field.  Only unknown identities
    # are reported here; the supported Personal + local pair is not a duplicate.
    duplicates = $unrecognizedIdentities
    action_required = ($unrecognizedIdentities.Count -gt 0)
    recommendation = if ($unrecognizedIdentities.Count -gt 0) {
        "An unrecognized BHM connector identity is enabled: $($unrecognizedIdentities -join ', '). Review it manually. Do not disable, install, or remove any BHM connector during a health check."
    } elseif ($knownCoexistence) {
        "Personal BHM plugin and the local workspace mirror are both enabled. This is a supported coexistence: leave both enabled. The host-owned mcp_servers.bhm registration remains the single MCP server."
    } elseif ($personalEnabled) {
        "Personal BHM plugin is enabled. Do not disable it automatically; it may be the path that refreshes Codex plugin and MCP discovery."
    } elseif ($localMirrorEnabled) {
        "Local workspace mirror is enabled. Do not auto-install or remove the Personal BHM plugin; test any identity cutover only in a separate fresh Codex session."
    } else {
        "No BHM connector plugin is enabled. This guard is read-only and will not change plugin state."
    }
}

$result | ConvertTo-Json -Depth 10
