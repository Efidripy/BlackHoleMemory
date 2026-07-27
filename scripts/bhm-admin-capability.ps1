<#
.SYNOPSIS
  Manage the local BHM destructive-operator capability without exposing it.

.DESCRIPTION
  This is the trusted local operator path for BHM_ADMIN_CAPABILITY. The value
  is stored only in the current process and the Windows User environment; it
  is never written to the repository, a receipt, a log, a URL, or a response.
  Mutating actions require -OperatorApproved so a status probe cannot create
  privilege accidentally.
#>
param(
  [ValidateSet('status', 'ensure', 'rotate', 'clear')]
  [string]$Action = 'status',
  [switch]$OperatorApproved
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function New-BhmAdminCapability {
  $bytes = New-Object byte[] 48
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try { $rng.GetBytes($bytes) }
  finally { $rng.Dispose() }
  return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Get-BhmCapabilityFingerprint([Parameter(Mandatory = $true)][string]$Value) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $digest = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Value))
    return ([BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()).Substring(0, 12)
  }
  finally { $sha.Dispose() }
}

function Get-EffectiveCapability {
  $process = [string][Environment]::GetEnvironmentVariable('BHM_ADMIN_CAPABILITY', 'Process')
  $user = [string][Environment]::GetEnvironmentVariable('BHM_ADMIN_CAPABILITY', 'User')
  if ($process.Length -ge 32) {
    return [pscustomobject]@{ value = $process; source = 'process'; process_present = $true; user_present = $user.Length -ge 32 }
  }
  if ($user.Length -ge 32) {
    return [pscustomobject]@{ value = $user; source = 'user'; process_present = $false; user_present = $true }
  }
  return [pscustomobject]@{ value = ''; source = 'none'; process_present = $false; user_present = $false }
}

function Assert-OperatorApproved {
  if (-not $OperatorApproved) {
    throw 'operator_approval_required: rerun with -OperatorApproved'
  }
}

switch ($Action) {
  'status' {
    $effective = Get-EffectiveCapability
    [pscustomobject]@{
      ok = $true
      action = 'status'
      configured = $effective.value.Length -ge 32
      process_present = $effective.process_present
      user_present = $effective.user_present
      source = $effective.source
      fingerprint = if ($effective.value) { Get-BhmCapabilityFingerprint $effective.value } else { $null }
      secret_exposed = $false
    } | ConvertTo-Json -Depth 4
    exit 0
  }
  'ensure' {
    Assert-OperatorApproved
    $effective = Get-EffectiveCapability
    $created = $false
    if ($effective.value.Length -lt 32) {
      $effective = [pscustomobject]@{ value = (New-BhmAdminCapability); source = 'generated'; process_present = $false; user_present = $false }
      [Environment]::SetEnvironmentVariable('BHM_ADMIN_CAPABILITY', $effective.value, 'User')
      $created = $true
    }
    $env:BHM_ADMIN_CAPABILITY = $effective.value
    [pscustomobject]@{
      ok = $true
      action = 'ensure'
      created = $created
      configured = $true
      fingerprint = Get-BhmCapabilityFingerprint $effective.value
      secret_exposed = $false
      restart_required = $true
    } | ConvertTo-Json -Depth 4
    exit 0
  }
  'rotate' {
    Assert-OperatorApproved
    $value = New-BhmAdminCapability
    [Environment]::SetEnvironmentVariable('BHM_ADMIN_CAPABILITY', $value, 'User')
    $env:BHM_ADMIN_CAPABILITY = $value
    [pscustomobject]@{
      ok = $true
      action = 'rotate'
      rotated = $true
      fingerprint = Get-BhmCapabilityFingerprint $value
      secret_exposed = $false
      restart_required = $true
    } | ConvertTo-Json -Depth 4
    exit 0
  }
  'clear' {
    Assert-OperatorApproved
    [Environment]::SetEnvironmentVariable('BHM_ADMIN_CAPABILITY', $null, 'User')
    Remove-Item Env:BHM_ADMIN_CAPABILITY -ErrorAction SilentlyContinue
    [pscustomobject]@{
      ok = $true
      action = 'clear'
      configured = $false
      secret_exposed = $false
      restart_required = $true
    } | ConvertTo-Json -Depth 4
    exit 0
  }
}
