# BlackHoleMemory v2.0.3 — "Clean Slate"

## Release identity

- release version: `2.0.3`; channel: `PURE`; runtime: `bhm-v2.0.3-PURE`; broker: `ipc-broker-v2.0.3`;
  UI: `Runtime v2.0.3-PURE`; plugin: `2.0.3`.

## Included changes

- **Privacy clean-up.** Personal paths, usernames and local project names were removed from all tracked files
  and from the repository history; release archives and the launcher executable are built from a neutral
  build directory so no build-machine paths are embedded.
- Everything from v2.0.0 - v2.0.2 is included: the large-module splits (the launcher, `code_tools_service`,
  `bhm_mcp`, `memory_repository`, `repository_index` and about forty more), and the resource optimizations
  (API server about 1 MB/s disk reads instead of ~190, projection worker 0.1 MB/s, lower memory, cheaper readiness
  and launcher polling).
- No API, data-contract or storage changes. SQLite stays authoritative.

## Validation

- full gate on the release commit; Ruff clean; release archive and executable scanned for personal data.
