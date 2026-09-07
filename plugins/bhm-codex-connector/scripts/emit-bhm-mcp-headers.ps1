[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Codex invokes this local helper for its HTTP MCP connection.  The packaged
# Windows desktop client may not inherit the user's environment, so resolve
# the existing user-scoped credential through the connector's canonical
# reader.  The only stdout value is the JSON header object consumed by Codex.
. (Join-Path $PSScriptRoot 'bhm-memory-common.ps1')

$token = Get-ConnectorCallerToken
@{ Authorization = "Bearer $token" } | ConvertTo-Json -Compress
