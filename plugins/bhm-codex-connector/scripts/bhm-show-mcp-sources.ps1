param(
    [string]$CodexConfigPath = "C:\Users\xman\.codex\config.toml",
    [switch]$AsJson
)

. (Join-Path $PSScriptRoot "bhm-memory-common.ps1")

function Get-ConfiguredMcpNamesFromToml {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    $names = @()
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match '^\s*\[mcp_servers\.([^\]]+)\]\s*$') {
            $names += $Matches[1]
        }
    }
    return ($names | Sort-Object -Unique)
}

$configured = Get-ConfiguredMcpNamesFromToml -Path $CodexConfigPath
$result = [pscustomobject]@{
    active_session_mcp = [pscustomobject]@{
        available = $false
        servers = @()
        note = "Live MCP attach is not introspected by this script. Verify attached MCP tools from the current Codex session."
    }
    configured_mcp = [pscustomobject]@{
        path = $CodexConfigPath
        servers = $configured
    }
    rule = "Attached MCP tools in the current Codex session are authoritative for live availability; config is only a known inventory."
}

if ($AsJson) {
    $result | ConvertTo-Json -Depth 8
    exit 0
}

Write-Host ($result | ConvertTo-Json -Depth 8)
