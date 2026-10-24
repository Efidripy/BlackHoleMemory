# BlackHoleMemory v2.0.2 — "Quiet Machine"

## Release identity

- release version: `2.0.2`; channel: `PURE`; runtime: `bhm-v2.0.2-PURE`; broker: `ipc-broker-v2.0.2`;
  UI: `Runtime v2.0.2-PURE`; plugin: `2.0.2`.

## Included changes (performance only, no behaviour change)

The API server was reading about 190 MB/s from disk (bursts of 385 MB/s, one core busy) and the projection worker
4.8 MB/s around the clock. Steady state now: server about 1 MB/s, worker 0.1 MB/s, server memory ~227 MB.

- `POST /bhm/agent-activity-rollup` (polled every 30 s by the launcher) counted observations through a join that
  fetched every wide event row (1.5 GB per call); it now counts from the covering index (14 MB) and is cached.
- The memory-store readiness `PRAGMA quick_check` (a full-database scan) re-ran after every database write; it now
  refreshes on a 15-minute TTL in the background.
- The projection/outbox/hook/LLM job queues no longer run an unindexed expired-lease UPDATE on every poll.
- Launcher-polled endpoints (`/bhm/profile`, telemetry, atlas snapshot, rollup) read almost nothing (covering
  index counts, mtime/TTL caches); `/health/ready` no longer builds the heavy mem0 plan per call (443 ms -> 0.05 ms).
- Lazy loading of `qdrant_client`, `mem0` and `langgraph`, a bounded mem0 instance cache and one shared embedding
  model in the worker cut server memory at import by ~56 MB.
- Launcher: cached local HTTP opener, reused telemetry thread pool, no repaint on unchanged status, bounded log reads.

## Validation

- full gate: 3211 passed, 3 skipped; Ruff clean; release built from a clean worktree.
