# BlackHoleMemory v1.8.4 — provider warm-up completion

## Release identity

- release version: `1.8.4`;
- channel: `PURE`;
- runtime: `bhm-v1.8.4-PURE`;
- broker: `ipc-broker-v1.8.4`;
- UI: `Runtime v1.8.4-PURE`;
- plugin: `1.8.4`.

## Included changes

This release includes the governed retrieval, context-tier, answer-quality,
launcher telemetry and memory-doctor improvements described in the v1.8.3
candidate notes. It also fixes the mandatory provider readiness probe: a valid
768-dimension embedding response is accepted inside a separately bounded
256 KiB embedding response budget. The optional chat probe remains independent.

`provider_ready` therefore reflects the embedding dependency BHM retrieval
actually needs, rather than the operator's selected chat model or an
undersized JSON reader. Shared writes remain disabled.

## Validation

The release candidate requires the full repository suite, static/public-tree
gates, release provenance validation, post-restart provider readiness and
read-only MCP/search smokes. Native Codex MCP attachment is client-session
local and separately observable.
