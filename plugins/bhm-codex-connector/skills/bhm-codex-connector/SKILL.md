---
name: bhm
description: Use when Codex should operate with the local BHM ritual on this Windows workspace, including preflight, checkpointing, hybrid session records, and live-memory-check collection.
---

# BHM Connector

Use this skill on non-trivial work when the session should rely on the local BHM ritual.

## Required ritual

1. Before any BHM REST ritual, search, task call, checkpoint, or diagnosis,
   inspect the actual tool catalog of the current chat.

   - If `mcp__bhm__bhm_health` is available, call it exactly once as the first
     BHM action. Report its compact native outcome before using any REST BHM
     wrapper. This is the current-chat proof and also renews the native
     Streamable HTTP session.
   - If no `mcp__bhm__*` tool is available, do not retry, restart, alter
     `config.toml`, or claim attachment. Continue the user task without BHM;
     REST runtime checks may be used only as separately labelled transport
     evidence.
   - A configured server, a REST health response, or a live lease from another
     client never substitutes for the native probe. The BHM server cannot make
     a missing tool catalog appear by calling itself.

2. Start the task-open ritual:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-memory-preflight.ps1 -Project blackholememory
```

If `plugin_guard.action_required` is `true`, report only the unrecognized
third-party connector identity and request an explicit operator decision. The
known Personal `bhm-marketplace` and local workspace mirror identities may
coexist; do not disable, install, or silently remove either one during the
ritual.

If runtime clarity is missing, run runtime discovery first through the workbench or:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action status
```

3. For durable changes, close with either:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-memory-checkpoint.ps1 -Project blackholememory -Done "<done>" -Next "<next>" -Checks "<checks>" -Risks "<risks>"
```

or

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-session-hybrid-record.ps1 -Project blackholememory -Title "<title>" -Done "<done>" -Next "<next>" -Checks "<checks>" -Risks "<risks>" -Decisions "<decisions>" -FilesTouched "<files>" -ConversationNotes "<summary>"
```

4. For a compact live verification bundle:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-run-live-memory-check.ps1 -Title "<title>"
```

When a closeout includes both a checkpoint and a session record, pass the same
deterministic `-UpsertKey` to both wrappers. This keeps one backing memory
aggregate while preserving the two typed artifacts.

## Windows-specific rules

- Prefer `-DataFile` over inline JSON when calling PowerShell wrappers that forward JSON payloads.
- Treat the workspace REST bridge as the reliable ingress for memory events; optional native Codex hooks remain non-critical.
- If the memory runtime was just restarted, assume a short startup window and prefer the shared wrappers over raw ad-hoc API calls.
- Preflight probes must use native BHM routes only: `/bhm/health` for fast health, `/health/ready` for readiness, and `/bhm/search` for recall.
- After any ritual, discovery, live-check, checkpoint, or session-record action, explicitly report a short chat confirmation in plain text:
  - `ok: preflight done`
  - `ok: runtime discovery done`
  - `ok: checkpoint saved`
  - `ok: session record saved`
  - `ok: live check bundle collected`
  Keep these confirmations short and deterministic.

## Strict MCP tool contract

`bhm_remember` accepts a flat FastMCP v1.3+ argument object. The IPC broker does not unwrap legacy wrapper payloads, shorthand query fields, limit aliases, or CSV/JSON-string aliases.

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["content"],
  "properties": {
    "content": { "type": "string", "minLength": 1 },
    "project": { "type": "string", "default": "e-github-workspace" },
    "memory_type": { "type": "string", "default": "workflow" },
    "concepts": {
      "type": "array",
      "items": { "type": "string", "minLength": 1 },
      "default": []
    },
    "files": {
      "type": "array",
      "items": { "type": "string", "minLength": 1 },
      "default": []
    },
    "metadata": {
      "type": "object",
      "additionalProperties": true,
      "default": null
    }
  }
}
```

## Current workspace stance

- baseline Codex hooks stay `6-event only`
- workspace bridge scripts are authoritative
- `BlackHoleMemory` routes on `http://127.0.0.1:8000/bhm/*` are the primary ritual surface
- `http://127.0.0.1:8000` is the local viewer and API surface
- preferred runtime profile on this workstation is `low-context`
- preferred operator flow is now:
  - `Connect`
  - run the read-only connector guard; leave the known Personal and local
    workspace identities enabled when it reports supported coexistence
  - `Start task ritual`
  - do the work
  - `Close task ritual`
  - optional `Live check`

## Profile switch

There is no dedicated plugin-card toggle yet. Use:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action low-context -RestartWorker
```

Use `-Action standard` or `-Action deep` when a wider context budget is
required; all three actions keep a reversible env backup.

Profile files are BHM-native: `BHM_CONTEXT_TOKEN_BUDGET`,
`BHM_RETRIEVAL_*`, `BHM_OBSERVATION_*`, `BHM_GRAPH_*` and
`BHM_CONTEXT_*`. Applying a profile creates a local backup and removes only the
known legacy `AGENTMEMORY_*`/unprefixed aliases from the selected env file.

Check current status:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action status
```

The plugin page itself now exposes clickable default prompt cards for:

- switching to `low-context`
- showing current profile status
- running a live memory check

Profile compare remains available through:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action compare
```
