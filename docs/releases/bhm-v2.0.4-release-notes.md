# BlackHoleMemory v2.0.4 — "Patched"

## Release identity

- release version: `2.0.4`; channel: `PURE`; runtime: `bhm-v2.0.4-PURE`; broker: `ipc-broker-v2.0.4`;
  UI: `Runtime v2.0.4-PURE`; plugin: `2.0.4`.

## Included changes

- **Dependency security update (Dependabot alerts #54-#57).** `fastmcp` 3.2.0 -> 3.4.10 (post-transform component
  authorization could be skipped on direct calls), `opentelemetry-api` 1.44.0 -> 1.45.1 (unbounded tracestate parsing),
  `setuptools` 83.0.0 -> 84.x (Windows path escape in legacy handling). Only the target packages and their direct
  new dependencies changed in `uv.lock`.
- The MCP catalog is unchanged: the same 207 tools with identical names, descriptions and schemas.
- No API, data-contract or storage changes. SQLite stays authoritative.

## Validation

- full gate on the release commit; Ruff clean; release archive and executable scanned for personal data.
