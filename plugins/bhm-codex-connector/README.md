# BHM Connector (for Windows)

Repository-owned Codex plugin that packages the current BHM ritual for this Windows workstation.

## What it includes

- `.codex-plugin/plugin.json`
- plugin-local helper scripts for the BHM ritual and runtime discovery
- a Codex skill for the workspace ritual
- helper scripts for install, activation, runtime discovery, and smoke checks

## Runtime contract

- primary API surface defaults to `http://127.0.0.1:8000/bhm/*`;
  the repository catalog `config/runtime-endpoints.json` and
  `BHM_BASE_URL`/`BHM_HOST`/`BHM_PORT` overrides are authoritative
- viewer uses the same resolved BHM API endpoint unless explicitly overridden
  by the runtime discovery configuration
- native MCP truth is owned only by the canonical Streamable HTTP SDK session;
  inspect `GET /bhm/mcp/http/status`
- first native-probe policy: when the current chat exposes
  `mcp__bhm__bhm_health`, the agent calls it exactly once before every other
  BHM action. If the catalog is absent, the task continues without BHM; no
  retry, runtime restart, configuration change, or REST-as-native claim is
  permitted. A BHM server cannot create a tool catalog that Codex did not
  attach to the chat
- the Streamable HTTP registry publishes bounded session, catalog and idle
  lifecycle state; DELETE or idle expiry releases `attached` immediately
- the supervisor publishes `bhm.mcp.timeout-contract.v1`; startup, protocol,
  tool and provider budgets are independent and unrelated MCP servers are not
  awaited
- the supervisor publishes `bhm.mcp.process-ownership.v1`; wrapper/supervisor/
  launcher/parent PIDs are bound to the lease, graceful shutdown is explicit,
  and orphan cleanup is dry-run-first and limited to proven BHM descendants
- ritual wrappers publish `bhm.mcp.rest-degraded.v1`; they keep
  `current_session_verified=false` because a separate PowerShell process cannot
  prove Codex chat identity. `native_probe_required=true` and
  `session_attribution=unverifiable_by_rest` make that boundary explicit. A
  live lease is reported as `native MCP lease observed; REST bridge cannot
  verify this chat identity`; a healthy idle transport is reported as
  `native MCP transport ready; no live lease observed by REST bridge`. Both
  require a native tool probe first; exact `MCP unavailable` is reserved for a
  transport probe failure/unavailable contour. Native retries remain zero
- scoped repair preview follows the same boundary: healthy idle HTTP returns
  `native_probe_required`; reload is reserved for reviewed adapter mutation or
  a native probe that still fails after runtime/config are healthy
- MCP registration is owned centrally by the host clients under the canonical
  server id `bhm`; this plugin deliberately does not ship a second MCP manifest
- default runtime profile: `low-context`
- normal operator flow:
  1. connect
  2. inspect native BHM catalog; call `bhm_health` if available
  3. start task ritual
  4. do the work
  5. close task ritual
  6. optionally run a live check

## Core scripts

- `scripts/bhm-memory-common.ps1`
- `scripts/bhm-memory-preflight.ps1`
- `scripts/bhm-memory-checkpoint.ps1`
- `scripts/bhm-session-hybrid-record.ps1`
- `scripts/bhm-run-live-memory-check.ps1`
- `scripts/bhm-show-mcp-sources.ps1`
- `scripts/bhm-doctor-activate.ps1`
- `scripts/bhm-plugin-duplicate-guard.ps1`

Checkpoint/session closeouts use one deterministic `upsert_key` when they
describe the same workflow. `bhm-memory-checkpoint.ps1` writes the first-class
`/bhm/checkpoint` artifact; `bhm-session-hybrid-record.ps1` writes
`/bhm/session-record` directly and never nests another checkpoint write.

## Connector identities and MCP ownership

Codex may show two enabled identities for the same `1.8.4` connector bundle:

- `bhm-codex-connector@bhm-marketplace` is the Personal plugin identity. It
  may cause Codex to reload plugin/MCP discovery, so it must never be disabled
  automatically during health checks or preflight.
- `bhm-codex-connector@bhm-local-marketplace` is the workspace development
  mirror used to build and synchronize the editable repository source.

This known pair is a supported coexistence, not an actionable duplicate. The
preflight and doctor run `bhm-plugin-duplicate-guard.ps1` only to report an
*unknown third* connector identity. The guard is read-only and never enables,
disables, installs, or removes a plugin. Test an identity cutover only in a
separate fresh Codex session with an explicit operator decision.

Neither plugin owns a second MCP server. The single native registration remains
host-owned as `mcp_servers.bhm`; plugin state and current-session native tool
availability are separate facts.

## Current scope

This plugin is aimed at a practical Windows install and operator flow:

- make BHM easier to attach inside Codex desktop
- keep the Windows-specific ritual close to the plugin bundle
- expose the same portable release operator used by the launcher

Read-only release diagnosis:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bhm-release-operator.ps1 -Action doctor -AsJson
```

`install`, `update` and `rollback` are fail-closed and require explicit
`-Confirm`; `-DryRun` is non-mutating. `native-attach` reports only the live
Streamable HTTP session state.

## Repository source

This plugin is shipped from:

`plugins/bhm-codex-connector`

`config/plugin-source.json` is the machine-readable source contract. The
repository bundle is the only editable source; workspace marketplace and local
Codex copies are generated targets. Build or refresh them with one command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-bhm-plugin-bundle.ps1 -Target workspace-marketplace -Force
```

To refresh both the workspace marketplace and the user-local install target:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-bhm-plugin-bundle.ps1 -Target all -Force
```

Codex-managed cache is audited separately and can be refreshed explicitly when
the desktop installation needs it:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-bhm-plugin-bundle.ps1 -Target codex-cache -Force
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\validate-bhm-plugin-drift.ps1 -RequireCache -AsJson
```

The generator stages each target, keeps a timestamped backup under
the local plugin-bundle backup directory, and replaces only
the generated target after the staged manifest is validated. Use `-DryRun` to
inspect destinations without writing.

The Control Deck installer copies the generated repository bundle to:

`%USERPROFILE%\.codex\plugins\local\bhm-codex-connector`

## MCP adapter manifest

The Codex TOML entry and Claude JSON entry are checked against the single
`config/mcp-registration.json` adapter contract. Run a read-only drift check or
a fixture canary before changing any client surface:

```powershell
uv run python .\scripts\generate-bhm-mcp-adapters.py --check --json
uv run python .\scripts\generate-bhm-mcp-adapters.py --canary --json
```

Apply requires the canary flag and creates an atomic backup; the manifest
reports the client-specific reload action and never claims that a client was
restarted automatically.

## Source links

- install package: `plugins/bhm-codex-connector`
- active BHM repository: the repository root

## One-command smoke bundle

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\generate-plugin-smoke-bundle.ps1 -Title "bhm-plugin-live-test"
```

Output:

`C:\Users\xman\.codex\plugin-data\bhm\runtime\logs\plugin-smoke\bhm\latest.json`

## Profile helpers

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action status
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action low-context -RestartWorker
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action standard -RestartWorker
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action deep -RestartWorker
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action compare
```

Profile values are stored under the BHM-native `BHM_*` namespace. Applying a
profile backs up the env file under
`%USERPROFILE%\.codex\plugin-data\bhm\runtime\logs\profile-backups` and
removes only the known legacy profile aliases.
