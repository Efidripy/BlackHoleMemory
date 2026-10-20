# BlackHoleMemory v1.8.5 — launcher and MCP reliability

## Release identity

- release version: `1.8.5`;
- channel: `PURE`;
- runtime: `bhm-v1.8.5-PURE`;
- broker: `ipc-broker-v1.8.5`;
- UI: `Runtime v1.8.5-PURE`;
- plugin: `1.8.5`.

## Included changes

This release incorporates the launcher, sidecar, health-probe and MCP contract
fixes from commits `63d4999`, `8580014` and `be28cdd`:

- the launcher preserves invalid MCP-client configuration files, keeps stdio
  writes intact, and does not create directories for clients that are not
  installed;
- launcher log rotation and telemetry failure deduplication/backoff improve
  long-running reliability;
- frozen launchers clean up only their own stale `%TEMP%\_MEI*` bundles;
- a frozen launcher inside a checkout uses one state root in that checkout;
- the launcher owns the projection sidecar, including status, start and stop;
- successful Qdrant health probes use a TTL cache;
- one long-lived projection worker uses a stop file and rotating logs;
- MCP contract digests are server-owned, and rehydrated sessions are marked
  `unverified` so they do not produce false drift reports;
- `mcp` is upgraded to `1.30.0` for the StreamableHTTP session-accumulation
  advisory, and `[dependency-groups] dev` is defined for the development
  environment.

## Validation

The release requires the full repository suite, static/public-tree gates,
release provenance validation, post-restart provider readiness and read-only
MCP/search smokes. Native Codex MCP attachment remains client-session local
and separately observable.
