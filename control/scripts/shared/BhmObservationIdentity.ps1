function Get-BhmPropertyValue {
    param(
        [AllowNull()]
        [object]$Value,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if ($null -eq $Value) { return $null }
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Get-BhmNormalizedText {
    param([AllowNull()][object]$Value)

    if ($null -eq $Value) { return $null }
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    return $text.Trim()
}

function Resolve-BhmTextCandidate {
    param(
        [AllowNull()][object]$ExplicitValue,
        [AllowNull()][object]$Payload,
        [string[]]$PayloadNames = @(),
        [string[]]$EnvironmentNames = @()
    )

    $explicitText = Get-BhmNormalizedText -Value $ExplicitValue
    if ($explicitText) {
        return [pscustomobject]@{ value = $explicitText; source = "explicit" }
    }

    foreach ($name in $PayloadNames) {
        $payloadText = Get-BhmNormalizedText -Value (Get-BhmPropertyValue -Value $Payload -Name $name)
        if ($payloadText) {
            return [pscustomobject]@{ value = $payloadText; source = "payload:$name" }
        }
    }

    foreach ($name in $EnvironmentNames) {
        $environmentText = Get-BhmNormalizedText -Value ([Environment]::GetEnvironmentVariable($name))
        if ($environmentText) {
            return [pscustomobject]@{ value = $environmentText; source = "env:$name" }
        }
    }

    return [pscustomobject]@{ value = $null; source = $null }
}

function ConvertTo-BhmIdentitySlug {
    param([string]$Value)

    $slug = ($Value -replace '[^A-Za-z0-9._-]+', '-').Trim('-').ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($slug)) { return "session" }
    return $slug
}

function Get-BhmSha256Token {
    param([Parameter(Mandatory = $true)][string]$Value)

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        $hash = $sha256.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash) -replace '-', '').ToLowerInvariant()
    } finally {
        $sha256.Dispose()
    }
}

function Get-BhmParentProcessIdentity {
    $parentPid = $null
    $parentName = $null
    $parentStartedAt = $null

    try {
        $current = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $PID" -ErrorAction Stop
        $parentPid = [int]$current.ParentProcessId
    } catch {
        $parentPid = $null
    }

    if ($parentPid) {
        try {
            $parent = Get-Process -Id $parentPid -ErrorAction Stop
            $parentName = $parent.ProcessName
            $parentStartedAt = $parent.StartTime.ToUniversalTime().ToString('o')
        } catch {
            $parentName = $null
            $parentStartedAt = $null
        }
    }

    return [pscustomobject]@{
        pid = $parentPid
        name = $parentName
        startedAt = $parentStartedAt
    }
}

function New-BhmStableProcessSessionId {
    param(
        [string]$Namespace = "agent",
        [string]$Cwd = ""
    )

    $parent = Get-BhmParentProcessIdentity
    $ambientSession = Resolve-BhmTextCandidate `
        -Payload $null `
        -EnvironmentNames @('WT_SESSION', 'TERM_SESSION_ID', 'VSCODE_PID', 'SESSIONNAME')
    $material = @(
        "namespace=$Namespace",
        "cwd=$Cwd",
        "user=$env:USERNAME",
        "parent_pid=$($parent.pid)",
        "parent_name=$($parent.name)",
        "parent_started_at=$($parent.startedAt)",
        "ambient_session=$($ambientSession.value)"
    ) -join '|'
    $token = Get-BhmSha256Token -Value $material
    $slug = ConvertTo-BhmIdentitySlug -Value $Namespace
    return "session_bhm_${slug}_$($token.Substring(0, 24))"
}

function New-BhmObservationEventId {
    return "obs_bhm_$([guid]::NewGuid().ToString('N'))"
}

function Resolve-BhmObservationIdentity {
    param(
        [AllowNull()][object]$Payload,
        [string]$SessionId,
        [string]$EventId,
        [string]$CorrelationId,
        [string]$ParentEventId,
        [string]$Cwd = "",
        [string]$SessionNamespace = "agent"
    )

    $session = Resolve-BhmTextCandidate `
        -ExplicitValue $SessionId `
        -Payload $Payload `
        -PayloadNames @(
            'session_id', 'sessionId',
            'agent_session_id', 'agentSessionId',
            'conversation_id', 'conversationId',
            'thread_id', 'threadId'
        ) `
        -EnvironmentNames @('CODEX_SESSION_ID', 'CLAUDE_SESSION_ID', 'CODEX_THREAD_ID')
    if (-not $session.value) {
        $session = [pscustomobject]@{
            value = New-BhmStableProcessSessionId -Namespace $SessionNamespace -Cwd $Cwd
            source = "fallback:parent-process"
        }
    }

    $event = Resolve-BhmTextCandidate `
        -ExplicitValue $EventId `
        -Payload $Payload `
        -PayloadNames @('event_id', 'eventId', 'hook_event_id', 'hookEventId')
    if (-not $event.value) {
        $event = [pscustomobject]@{
            value = New-BhmObservationEventId
            source = "generated:guid"
        }
    }

    $correlation = Resolve-BhmTextCandidate `
        -ExplicitValue $CorrelationId `
        -Payload $Payload `
        -PayloadNames @(
            'correlation_id', 'correlationId',
            'task_id', 'taskId',
            'turn_id', 'turnId',
            'thread_id', 'threadId',
            'conversation_id', 'conversationId'
        ) `
        -EnvironmentNames @('CODEX_THREAD_ID', 'CODEX_TASK_ID')
    if (-not $correlation.value) {
        $correlation = [pscustomobject]@{
            value = $session.value
            source = "fallback:sessionId"
        }
    }

    $parent = Resolve-BhmTextCandidate `
        -ExplicitValue $ParentEventId `
        -Payload $Payload `
        -PayloadNames @('parent_event_id', 'parentEventId', 'parent_id', 'parentId')

    return [pscustomobject]@{
        sessionId = $session.value
        sessionSource = $session.source
        eventId = $event.value
        eventSource = $event.source
        correlationId = $correlation.value
        correlationSource = $correlation.source
        parentEventId = $parent.value
        parentEventSource = $parent.source
    }
}
