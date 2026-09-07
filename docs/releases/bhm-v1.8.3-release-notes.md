# BlackHoleMemory v1.8.3 — governed retrieval and launcher clarity

## Release identity

- release version: `1.8.3`;
- channel: `PURE`;
- runtime: `bhm-v1.8.3-PURE`;
- broker: `ipc-broker-v1.8.3`;
- UI: `Runtime v1.8.3-PURE`;
- plugin: `1.8.3`.

## Included changes

- Keeps SQLite authoritative while extending governed, project-scoped shared
  reads; shared writes remain disabled.
- Adds bounded context-tier and answer-quality evaluation surfaces with explicit
  evidence boundaries.
- Makes mandatory provider readiness test the configured embedding endpoint;
  optional chat diagnostics no longer make memory readiness depend on the
  operator's active chat model.
- Makes launcher telemetry distinguish a ready MCP endpoint from an attached
  client lease.
- Preserves a redacted authoritative owner digest in memory-doctor snapshots,
  so projection parity can verify governed records without exposing owner IDs.

## Validation

The release candidate is accepted only after the repository test, static, public
tree, release-build and post-restart smoke gates succeed. A native Codex MCP
attachment remains session-local and must be verified in a newly attached client.
