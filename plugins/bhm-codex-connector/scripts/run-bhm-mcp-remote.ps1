[CmdletBinding()]
param(
    [string]$McpUrl = "http://127.0.0.1:8000/mcp"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'bhm-memory-common.ps1')

try {
    $uri = [System.Uri]$McpUrl
} catch {
    throw 'MCP URL must be a loopback URL'
}

$hostName = $uri.Host.Trim('[', ']')
$isLoopback = $hostName -eq 'localhost'
if (-not $isLoopback) {
    $ipAddress = $null
    if ([System.Net.IPAddress]::TryParse($hostName, [ref]$ipAddress)) {
        $isLoopback = [System.Net.IPAddress]::IsLoopback($ipAddress)
    }
}

if ($uri.Scheme -notin @('http', 'https') -or -not $isLoopback) {
    throw 'MCP URL must be a loopback URL'
}

$env:BHM_AUTH = "Bearer $(Get-ConnectorMcpBearer)"
$process = Start-Process -FilePath 'npx.cmd' -NoNewWindow -Wait -PassThru -ArgumentList @(
    '-y',
    'mcp-remote@0.15.1',
    $McpUrl,
    '--header',
    'Authorization:${BHM_AUTH}'
)
exit $process.ExitCode
