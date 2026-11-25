# BlackHoleMemory v2.1.1 — "Warm-up Fix"

## Release identity

- release version: `2.1.1`; channel: `PURE`; runtime: `bhm-v2.1.1-PURE`; broker: `ipc-broker-v2.1.1`;
  UI: `Runtime v2.1.1-PURE`; plugin: `2.1.1`.

## Included changes

- **Embeddings no longer follow a global `OPENAI_BASE_URL`.** The API start scripts point memory embeddings at
  the local LM Studio endpoint. A chat proxy in `OPENAI_BASE_URL` has no `/embeddings` and kept provider warm-up in
  `RETRYING`. To use another embedding server, set `BHM_MEM0_OPENAI_BASE_URL`.
- **Hook retries no longer fail as collisions.** A redelivered observation that differs only by ingest time is the
  same event, so a retried compact hook can finish instead of keeping the SLO `BREACHED`.
- **Launcher.** The Provider Warm-up tile shows the last error in its tooltip. Paid mode hides the local port and no
  longer probes an empty remote URL. The LLM card has no start/stop/restart buttons (they did nothing).
- No change to the MCP catalog, API contracts or storage. SQLite stays authoritative.

## Upgrade note

- Jobs that already failed with `observation_event_id_collision` stay failed; they are not requeued automatically.

## Validation

- unit suite plus core integration: 3210 passed, 3 skipped, 2 failed (module-size ratchet and resource callsite
  inventory; both failed before these changes). Run with `BHM_*` and `OPENAI_*` environment variables unset.
- local service restart: provider warm-up `ready` on the first attempt, SLO `healthy`.
