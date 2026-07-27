param(
    [string]$ConfigPath = "C:\Users\xman\.codex\config.toml",
    [string]$MarketplaceName = "bhm-marketplace",
    [string]$MarketplaceRoot = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($MarketplaceRoot)) {
    $MarketplaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Codex config not found: $ConfigPath"
}

$text = Get-Content -Raw -LiteralPath $ConfigPath -Encoding UTF8
$sectionHeader = "[marketplaces.$MarketplaceName]"

if ($text.Contains($sectionHeader)) {
    Write-Host "Marketplace already configured: $MarketplaceName"
    exit 0
}

$entry = @"

[marketplaces.$MarketplaceName]
last_updated = "$(Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")"
source_type = "local"
source = '\\?\$MarketplaceRoot'
"@

$updated = $text.TrimEnd() + "`r`n" + $entry + "`r`n"
$encoding = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($ConfigPath, $updated, $encoding)

Write-Host "Configured local marketplace: $MarketplaceName"
Write-Host "- root: $MarketplaceRoot"
Write-Host "- config: $ConfigPath"
