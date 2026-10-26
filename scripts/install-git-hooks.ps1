# Installs the tracked privacy pre-commit hook into this clone (shared by all its worktrees).
# Existing hooks (Git LFS, post-commit) stay untouched. Run once per clone.
$ErrorActionPreference = 'Stop'
$root = git -C $PSScriptRoot rev-parse --show-toplevel
$hooks = Join-Path (git -C $root rev-parse --path-format=absolute --git-common-dir) 'hooks'
$src = Join-Path $root 'scripts/git-hooks/pre-commit'
# LF only: a CRLF checkout (autocrlf) would break the sh shebang.
$text = (Get-Content -LiteralPath $src -Raw) -replace "`r`n", "`n"
[IO.File]::WriteAllText((Join-Path $hooks 'pre-commit'), $text, [Text.UTF8Encoding]::new($false))
Write-Output "installed: $(Join-Path $hooks 'pre-commit')"
