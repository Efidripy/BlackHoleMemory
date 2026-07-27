param(
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $scriptRoot "set-bhm-profile.ps1"

& powershell -NoProfile -ExecutionPolicy Bypass -File $target -Profile low-context -RestartWorker:$true -AsJson:$AsJson
