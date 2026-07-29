[CmdletBinding(DefaultParameterSetName = "ScriptPath")]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Node", "PowerShell")]
    [string]$Runtime,

    [Parameter(ParameterSetName = "ScriptPath", Mandatory = $true)]
    [string]$ScriptPath,

    [Parameter(ParameterSetName = "ScriptText", Mandatory = $true)]
    [string]$ScriptText,

    [string]$WorkingDirectory = "",
    [string[]]$Arguments = @()
)

$ErrorActionPreference = "Stop"

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$previousInputEncoding = [Console]::InputEncoding
$previousOutputEncoding = [Console]::OutputEncoding
$tempPath = $null
$exitCode = 0

function Resolve-RunnerWorkingDirectory {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ""
    }

    return (Resolve-Path -LiteralPath $Path).Path
}

function Resolve-RunnerScriptPath {
    param(
        [string]$Path,
        [string]$BaseDirectory
    )

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return (Resolve-Path -LiteralPath $Path).Path
    }

    if (-not [string]::IsNullOrWhiteSpace($BaseDirectory)) {
        return (Resolve-Path -LiteralPath (Join-Path $BaseDirectory $Path)).Path
    }

    return (Resolve-Path -LiteralPath $Path).Path
}

function Get-PowerShellRunnerExecutable {
    $executableName = if ($PSVersionTable.PSEdition -eq "Core") { "pwsh" } else { "powershell" }
    $candidate = Join-Path $PSHOME "$executableName.exe"
    if (Test-Path -LiteralPath $candidate) {
        return $candidate
    }
    return $executableName
}

function New-Utf8TempScript {
    param(
        [string]$Content,
        [string]$Extension
    )

    $path = Join-Path ([System.IO.Path]::GetTempPath()) ("codex-utf8-{0}{1}" -f [guid]::NewGuid().ToString("N"), $Extension)
    [System.IO.File]::WriteAllText($path, $Content, $utf8NoBom)
    return $path
}

try {
    [Console]::InputEncoding = $utf8NoBom
    [Console]::OutputEncoding = $utf8NoBom

    $resolvedWorkingDirectory = Resolve-RunnerWorkingDirectory -Path $WorkingDirectory

    if ($PSCmdlet.ParameterSetName -eq "ScriptText") {
        $extension = if ($Runtime -eq "PowerShell") { ".ps1" } else { ".js" }
        $tempPath = New-Utf8TempScript -Content $ScriptText -Extension $extension
        $resolvedScriptPath = $tempPath
    }
    else {
        $resolvedScriptPath = Resolve-RunnerScriptPath -Path $ScriptPath -BaseDirectory $resolvedWorkingDirectory
    }

    if (-not [string]::IsNullOrWhiteSpace($resolvedWorkingDirectory)) {
        Push-Location $resolvedWorkingDirectory
    }

    try {
        if ($Runtime -eq "Node") {
            & node $resolvedScriptPath @Arguments
        }
        else {
            $powershellExe = Get-PowerShellRunnerExecutable
            & $powershellExe -NoProfile -ExecutionPolicy Bypass -File $resolvedScriptPath @Arguments
        }

        if ($null -ne $global:LASTEXITCODE) {
            $exitCode = $global:LASTEXITCODE
        }
    }
    finally {
        if (-not [string]::IsNullOrWhiteSpace($resolvedWorkingDirectory)) {
            Pop-Location
        }
    }
}
finally {
    [Console]::InputEncoding = $previousInputEncoding
    [Console]::OutputEncoding = $previousOutputEncoding

    if ($tempPath -and (Test-Path -LiteralPath $tempPath)) {
        Remove-Item -LiteralPath $tempPath -Force
    }
}

if ($exitCode -ne 0) {
    exit $exitCode
}
