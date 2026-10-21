# BlackHoleMemory v1.8.6 — MCP memory writes for Claude and Codex

## Release identity

- release version: `1.8.6`;
- channel: `PURE`;
- runtime: `bhm-v1.8.6-PURE`;
- broker: `ipc-broker-v1.8.6`;
- UI: `Runtime v1.8.6-PURE`;
- plugin: `1.8.6`.

## Included changes

- **`bhm_remember` over MCP.** A `/mcp` call writes memory as the operator
  (the same path as REST `/bhm/remember`) when the client presents the operator
  capability (`BHM_ADMIN_CAPABILITY`) as the bearer or in
  `x-bhm-admin-capability`. The decision is made per `tools/call` from that
  request's HTTP headers and is reset after the dispatch. Clients without the
  capability, the IPC broker, and capability values placed in `_meta` or tool
  arguments are still rejected as non-authoritative ingress, and the rejection
  names the header state (`operator_header`: `absent`/`invalid`) without ever
  returning a value. Operator writes keep the secret-like input, prompt-injection
  and project checks.
- **`concepts: null` / `files: null`** are accepted by the `bhm_remember` contract.
- **Capability provisioning.** The service start path creates
  `BHM_ADMIN_CAPABILITY` (32 random bytes, hex) in the user environment when it
  is missing; an existing value is never overwritten and the value is never
  printed (`Rotate-BhmAdminCapability` rotates it explicitly).
- **Claude Desktop.** The launcher writes the `bhm` entry as a stdio bridge
  (`plugins/bhm-codex-connector/scripts/run-bhm-mcp-remote.ps1`, `mcp-remote@0.15.1`)
  that sends the capability as the bearer and falls back to the caller token;
  the entry replaces an older `url`/`bearer_token_env_var`/`env` form (the previous
  file is kept as `.bhm-backup`). The Integrations window reports
  "Operator write: ready/unavailable".
- **Codex.** The plugin header helper now emits the capability as the bearer
  (caller token fallback); a process-level value wins over the user environment.
- **Other.** Cursor targets and every other route are unchanged; the capability
  is accepted as a bearer on `/mcp` only.

## Operator notes

- Set `bearer_token_env_var = "BHM_ADMIN_CAPABILITY"` in the Codex `[mcp_servers.bhm]`
  section next to `http_headers_helper` (see the caller-token runbook).
- Claude Desktop forwards only the `Authorization` header to a remote server; a
  separate capability header never arrives, which is why the bearer form is used.
- The Integrations window "Inject" action replaces a custom `bhm` entry in
  `claude_desktop_config.json`; the previous file is saved next to it.

## Validation

- full pytest gate, Ruff, public-tree and docs-link checks (see the release commit);
- live: Codex and Claude Desktop wrote and archived test memories through MCP.
