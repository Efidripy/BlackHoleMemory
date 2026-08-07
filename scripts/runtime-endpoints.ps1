function Get-BhmRuntimeEndpointCatalog {
    param([string]$RepoRoot = "")
    $configured = [string]$env:BHM_RUNTIME_ENDPOINTS_FILE
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($configured)) { $candidates += $configured }
    if (-not [string]::IsNullOrWhiteSpace($RepoRoot)) { $candidates += (Join-Path $RepoRoot 'config\runtime-endpoints.json') }
    $candidates += (Join-Path (Split-Path -Parent $PSScriptRoot) 'config\runtime-endpoints.json')
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (Test-Path -LiteralPath $candidate) {
            return (Get-Content -Raw -LiteralPath $candidate -Encoding UTF8 | ConvertFrom-Json)
        }
    }
    throw 'runtime-endpoints.json was not found'
}

function Get-BhmRuntimeEndpoint {
    param(
        [Parameter(Mandatory)][string]$Name,
        [string]$RepoRoot = '',
        [string]$Path = ''
    )
    $catalog = Get-BhmRuntimeEndpointCatalog -RepoRoot $RepoRoot
    $service = $catalog.services.$Name
    if ($null -eq $service) { throw "Unknown BHM endpoint service: $Name" }
    $readEnv = {
        param([string]$Name)
        if ([string]::IsNullOrWhiteSpace($Name)) { return '' }
        # Environment providers can expose both Path and PATH after a
        # launcher/PowerShell handoff; Get-Item then throws a duplicate-key
        # error and prevents the authoritative API from starting. Read the
        # process block directly so endpoint resolution is case-insensitive
        # and deterministic across Windows launch paths.
        return [string][Environment]::GetEnvironmentVariable($Name, 'Process')
    }
    $explicit = if ($service.url_env) { & $readEnv ([string]$service.url_env) } else { '' }
    if ([string]::IsNullOrWhiteSpace($explicit)) {
        $hostValue = & $readEnv ([string]$service.host_env)
        if ([string]::IsNullOrWhiteSpace($hostValue)) { $hostValue = [string]$service.host }
        $portValue = & $readEnv ([string]$service.port_env)
        if ([string]::IsNullOrWhiteSpace($portValue)) { $portValue = [string]$service.port }
        $explicit = "{0}://{1}:{2}{3}" -f $service.scheme, $hostValue, $portValue, [string]$service.base_path
    }
    $explicit = $explicit.TrimEnd('/')
    if ([string]::IsNullOrWhiteSpace($Path)) { return $explicit }
    return "$explicit/$($Path.TrimStart('/'))"
}

function Get-BhmRuntimeEndpointParts {
    param([Parameter(Mandatory)][string]$Name, [string]$RepoRoot = '')
    $uri = [Uri](Get-BhmRuntimeEndpoint -Name $Name -RepoRoot $RepoRoot)
    return [pscustomobject]@{ Host = $uri.Host; Port = $uri.Port }
}

function Assert-BhmApiLoopbackHost {
    param([Parameter(Mandatory)][string]$HostName)
    $normalized = $HostName.Trim().TrimEnd('.').ToLowerInvariant()
    if ($normalized -in @('localhost', 'localhost.localdomain')) { return }
    $address = $null
    if ([System.Net.IPAddress]::TryParse($normalized, [ref]$address) -and [System.Net.IPAddress]::IsLoopback($address)) {
        return
    }
    throw 'BHM API listener host must be loopback-only (localhost, 127.0.0.1, or ::1)'
}
