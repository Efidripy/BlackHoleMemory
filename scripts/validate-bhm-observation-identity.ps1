param(
    [string]$WorkspaceRoot = "E:\GitHub",
    [string]$RepoPath = "E:\GitHub\repos\BlackHoleMemory",
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

$hookScript = Join-Path $WorkspaceRoot "workspace\control\scripts\shared\invoke-bhm-codex-hook.ps1"
$bridgeScript = Join-Path $WorkspaceRoot "workspace\control\scripts\shared\invoke-workspace-bhm-bridge.ps1"
$postCommitScript = Join-Path $WorkspaceRoot "workspace\control\scripts\shared\invoke-bhm-post-commit.ps1"
$senderScript = Join-Path $WorkspaceRoot "workspace\control\scripts\shared\send-bhm-observe.ps1"
$identityScript = Join-Path $RepoPath "control\scripts\shared\BhmObservationIdentity.ps1"
$results = [System.Collections.Generic.List[object]]::new()

function Add-Check {
    param(
        [string]$Name,
        [bool]$Passed,
        [string]$Detail
    )

    $results.Add([pscustomobject]@{
        check = $Name
        status = if ($Passed) { "PASS" } else { "FAIL" }
        detail = $Detail
    }) | Out-Null
}

function ConvertFrom-ChildJson {
    param(
        [string[]]$Output,
        [string]$Context,
        [int]$ExitCode
    )

    if ($ExitCode -ne 0) {
        throw "$Context failed with exit code $ExitCode`: $($Output -join [Environment]::NewLine)"
    }
    return (($Output -join [Environment]::NewLine) | ConvertFrom-Json)
}

function Invoke-HookDryRun {
    param(
        [string]$ScriptName,
        [string]$PayloadJson
    )

    $output = @($PayloadJson | & powershell -NoProfile -ExecutionPolicy Bypass -File $hookScript -ScriptName $ScriptName -DryRun 2>&1)
    return ConvertFrom-ChildJson -Output $output -Context "hook dry-run $ScriptName" -ExitCode $LASTEXITCODE
}

function Invoke-ScriptDryRun {
    param(
        [string]$ScriptPath,
        [string[]]$Arguments
    )

    $output = @(& powershell -NoProfile -ExecutionPolicy Bypass -File $ScriptPath @Arguments 2>&1)
    return ConvertFrom-ChildJson -Output $output -Context "dry-run $ScriptPath" -ExitCode $LASTEXITCODE
}

$requiredFiles = @($hookScript, $bridgeScript, $postCommitScript, $senderScript, $identityScript)
foreach ($file in $requiredFiles) {
    Add-Check -Name "file:$([IO.Path]::GetFileName($file))" -Passed (Test-Path -LiteralPath $file) -Detail $file
}

foreach ($file in ($requiredFiles | Where-Object { Test-Path -LiteralPath $_ })) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$parseErrors) | Out-Null
    Add-Check `
        -Name "syntax:$([IO.Path]::GetFileName($file))" `
        -Passed (@($parseErrors).Count -eq 0) `
        -Detail $(if (@($parseErrors).Count -eq 0) { "PowerShell parser clean" } else { (@($parseErrors | ForEach-Object Message) -join '; ') })
}

$explicitFirst = Invoke-HookDryRun `
    -ScriptName "prompt-submit.mjs" `
    -PayloadJson '{"session_id":"agent-session-001","correlation_id":"task-001","event_id":"event-001","parent_event_id":"parent-001","cwd":"E:\\GitHub\\repos\\BlackHoleMemory"}'
$explicitSecond = Invoke-HookDryRun `
    -ScriptName "post-tool-use.mjs" `
    -PayloadJson '{"session_id":"agent-session-001","correlation_id":"task-001","cwd":"E:\\GitHub\\repos\\BlackHoleMemory"}'

Add-Check -Name "hook_preserves_session" -Passed ($explicitFirst.sessionId -eq "agent-session-001" -and $explicitSecond.sessionId -eq $explicitFirst.sessionId) -Detail "same external agent session across hook events"
Add-Check -Name "hook_preserves_correlation" -Passed ($explicitFirst.correlationId -eq "task-001" -and $explicitSecond.correlationId -eq "task-001") -Detail "task correlation preserved"
Add-Check -Name "hook_preserves_event_lineage" -Passed ($explicitFirst.eventId -eq "event-001" -and $explicitFirst.parentEventId -eq "parent-001") -Detail "explicit event and parent IDs preserved"
Add-Check -Name "hook_generates_unique_event" -Passed ($explicitSecond.eventId -match '^obs_bhm_[0-9a-f]{32}$' -and $explicitSecond.eventId -ne $explicitFirst.eventId) -Detail "missing event ID receives a full GUID-based ID"

$identityEnvironmentNames = @('CODEX_SESSION_ID', 'CLAUDE_SESSION_ID', 'CODEX_THREAD_ID')
$previousEnvironment = @{}
foreach ($name in $identityEnvironmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    [Environment]::SetEnvironmentVariable($name, $null, 'Process')
}

try {
    $fallbackFirst = Invoke-HookDryRun -ScriptName "pre-tool-use.mjs" -PayloadJson '{"cwd":"E:\\GitHub\\repos\\BlackHoleMemory"}'
    $fallbackSecond = Invoke-HookDryRun -ScriptName "post-tool-use.mjs" -PayloadJson '{"cwd":"E:\\GitHub\\repos\\BlackHoleMemory"}'
} finally {
    foreach ($name in $identityEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
}

Add-Check -Name "fallback_session_is_process_stable" -Passed ($fallbackFirst.sessionSource -eq 'fallback:parent-process' -and $fallbackFirst.sessionId -eq $fallbackSecond.sessionId) -Detail "fallback is stable for sibling hook processes"
Add-Check -Name "fallback_events_are_unique" -Passed ($fallbackFirst.eventId -ne $fallbackSecond.eventId) -Detail "fallback session does not collapse event identity"

$tempDataFile = Join-Path $env:TEMP ("bhm-observation-identity-{0}.json" -f [guid]::NewGuid().ToString('N'))
try {
    [IO.File]::WriteAllText(
        $tempDataFile,
        '{"task_id":"workspace-task-001"}',
        [Text.UTF8Encoding]::new($false)
    )

    $sender = Invoke-ScriptDryRun -ScriptPath $senderScript -Arguments @(
        '-HookType', 'workspace_sender_test',
        '-SessionId', 'sender-session-001',
        '-EventId', 'sender-event-001',
        '-CorrelationId', 'sender-task-001',
        '-ParentEventId', 'sender-parent-001',
        '-Project', 'blackholememory',
        '-Cwd', $RepoPath,
        '-Source', 'identity-validator',
        '-DataFile', $tempDataFile,
        '-DryRun'
    )
    Add-Check -Name "sender_emits_v1_identity" -Passed (
        $sender.payload.schemaVersion -eq '1.0' -and
        $sender.payload.eventId -eq 'sender-event-001' -and
        $sender.payload.sessionId -eq 'sender-session-001' -and
        $sender.payload.correlationId -eq 'sender-task-001' -and
        $sender.payload.parentEventId -eq 'sender-parent-001'
    ) -Detail "generic sender emits the full v1 identity envelope"

    $bridgeFirst = Invoke-ScriptDryRun -ScriptPath $bridgeScript -Arguments @(
        '-EventName', 'workspace_test_started',
        '-Project', 'blackholememory',
        '-Cwd', $RepoPath,
        '-SessionId', 'workspace-flow-001',
        '-DataFile', $tempDataFile,
        '-DryRun'
    )
    $bridgeSecond = Invoke-ScriptDryRun -ScriptPath $bridgeScript -Arguments @(
        '-EventName', 'workspace_test_finished',
        '-Project', 'blackholememory',
        '-Cwd', $RepoPath,
        '-SessionId', 'workspace-flow-001',
        '-DataFile', $tempDataFile,
        '-DryRun'
    )
    Add-Check -Name "bridge_preserves_flow_identity" -Passed (
        $bridgeFirst.payload.sessionId -eq 'workspace-flow-001' -and
        $bridgeSecond.payload.sessionId -eq $bridgeFirst.payload.sessionId -and
        $bridgeFirst.payload.correlationId -eq 'workspace-task-001'
    ) -Detail "workspace bridge keeps one session and task correlation across events"
    Add-Check -Name "bridge_events_are_unique" -Passed ($bridgeFirst.payload.eventId -ne $bridgeSecond.payload.eventId) -Detail "workspace bridge emits one event ID per event"
} finally {
    Remove-Item -LiteralPath $tempDataFile -Force -ErrorAction SilentlyContinue
}

$postCommitFirst = Invoke-ScriptDryRun -ScriptPath $postCommitScript -Arguments @('-RepoPath', $RepoPath, '-DryRun')
$postCommitSecond = Invoke-ScriptDryRun -ScriptPath $postCommitScript -Arguments @('-RepoPath', $RepoPath, '-DryRun')
$expectedCommit = (& git -C $RepoPath rev-parse --verify HEAD 2>$null | Select-Object -First 1)
Add-Check -Name "git_hook_uses_repo_session" -Passed ($postCommitFirst.payload.sessionId -eq $postCommitSecond.payload.sessionId -and $postCommitFirst.payload.sessionId -match '^git:blackholememory:[0-9a-f]{16}$') -Detail "post-commit session identifies the repository, not wall-clock time"
Add-Check -Name "git_hook_correlates_commit" -Passed ($postCommitFirst.payload.correlationId -eq "git:$expectedCommit") -Detail "post-commit correlation identifies HEAD"
Add-Check -Name "git_hook_events_are_unique" -Passed ($postCommitFirst.payload.eventId -ne $postCommitSecond.payload.eventId) -Detail "each post-commit signal has a unique event ID"

$passCount = @($results | Where-Object status -eq 'PASS').Count
$failCount = @($results | Where-Object status -eq 'FAIL').Count
$summary = [pscustomobject]@{
    success = ($failCount -eq 0)
    pass = $passCount
    fail = $failCount
    checks = $results
}

if ($AsJson) {
    $summary | ConvertTo-Json -Depth 20
} else {
    foreach ($result in $results) {
        Write-Host "[$($result.status)] $($result.check): $($result.detail)"
    }
    Write-Host "Summary: PASS=$passCount FAIL=$failCount"
}

if ($failCount -gt 0) {
    exit 1
}
