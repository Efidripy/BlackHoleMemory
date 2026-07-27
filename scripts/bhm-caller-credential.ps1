Set-StrictMode -Version Latest

function New-BhmCallerToken {
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try { $rng.GetBytes($bytes) }
  finally { $rng.Dispose() }
  return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Get-BhmCallerTokenFingerprint([Parameter(Mandatory = $true)][string]$Token) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $digest = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Token))
    return ([BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()).Substring(0, 12)
  }
  finally { $sha.Dispose() }
}

function Set-BhmCallerDefaults {
  if (-not $env:BHM_CALLER_ID) {
    $env:BHM_CALLER_ID = 'local-operator'
  }
  if (-not $env:BHM_CALLER_PROJECTS) {
    $env:BHM_CALLER_PROJECTS = '*'
  }
  if (-not $env:BHM_CALLER_DEFAULT_PROJECT) {
    $env:BHM_CALLER_DEFAULT_PROJECT = 'blackholememory'
  }
}

function Initialize-BhmCallerCredential {
  [CmdletBinding()]
  param([switch]$NoPersist)

  $processToken = [string]$env:BHM_CALLER_TOKEN
  $userToken = [string][Environment]::GetEnvironmentVariable('BHM_CALLER_TOKEN', 'User')
  # The persisted user credential is the local caller authority. Prefer it
  # when a stale parent process carries a different token after reboot or
  # rotation; otherwise the runtime and launcher silently disagree.
  if ($userToken.Length -ge 32) {
    $token = $userToken
    $source = if ($processToken.Length -ge 32 -and $processToken -ne $userToken) { 'user-reconciled' } else { 'user' }
  } else {
    $token = $processToken
    $source = 'process'
  }
  if ($token.Length -lt 32) {
    $token = New-BhmCallerToken
    $source = 'generated'
    if (-not $NoPersist) {
      [Environment]::SetEnvironmentVariable('BHM_CALLER_TOKEN', $token, 'User')
      [Environment]::SetEnvironmentVariable('BHM_CALLER_ID', 'local-operator', 'User')
      [Environment]::SetEnvironmentVariable('BHM_CALLER_PROJECTS', '*', 'User')
      [Environment]::SetEnvironmentVariable('BHM_CALLER_DEFAULT_PROJECT', 'blackholememory', 'User')
    }
  }

  $env:BHM_CALLER_TOKEN = $token
  foreach ($name in @('BHM_CALLER_ID', 'BHM_CALLER_PROJECTS', 'BHM_CALLER_DEFAULT_PROJECT')) {
    if (-not (Get-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue)) {
      $userValue = [string][Environment]::GetEnvironmentVariable($name, 'User')
      if ($userValue) { Set-Item -LiteralPath "Env:$name" -Value $userValue }
    }
  }
  Set-BhmCallerDefaults

  return [pscustomobject]@{
    ok = $true
    source = $source
    fingerprint = Get-BhmCallerTokenFingerprint -Token $token
    caller_id = $env:BHM_CALLER_ID
    project_scope = $env:BHM_CALLER_PROJECTS
    persisted = -not $NoPersist
  }
}

function Get-BhmCallerCredentialStatus {
  $processToken = [string]$env:BHM_CALLER_TOKEN
  $userToken = [string][Environment]::GetEnvironmentVariable('BHM_CALLER_TOKEN', 'User')
  $effective = if ($processToken.Length -ge 32) { $processToken } else { $userToken }
  return [pscustomobject]@{
    configured = $effective.Length -ge 32
    process_present = $processToken.Length -ge 32
    user_present = $userToken.Length -ge 32
    fingerprint = if ($effective.Length -ge 32) { Get-BhmCallerTokenFingerprint -Token $effective } else { $null }
  }
}

function Rotate-BhmCallerCredential {
  [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
  param()

  if (-not $PSCmdlet.ShouldProcess('BHM caller credential', 'Rotate user and process token')) {
    return [pscustomobject]@{ ok = $false; rotated = $false }
  }
  $token = New-BhmCallerToken
  [Environment]::SetEnvironmentVariable('BHM_CALLER_TOKEN', $token, 'User')
  $env:BHM_CALLER_TOKEN = $token
  Set-BhmCallerDefaults
  return [pscustomobject]@{
    ok = $true
    rotated = $true
    fingerprint = Get-BhmCallerTokenFingerprint -Token $token
    client_restart_required = $true
  }
}
