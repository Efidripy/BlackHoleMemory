# BlackHoleMemory v2.0.1 — "Monster Dismantled, Part Two"

## Release identity

- release version: `2.0.1`;
- channel: `PURE`;
- runtime: `bhm-v2.0.1-PURE`;
- broker: `ipc-broker-v2.0.1`;
- UI: `Runtime v2.0.1-PURE`;
- plugin: `2.0.1`.

## Included changes

- **No behaviour change: a second, larger wave of module splits.** About forty
  oversized modules were cut into cohesive pieces (code moved verbatim, verified by
  comparing every definition with the original). Largest effects: the launcher
  `bhm_launcher.py` 4487 -> 870 lines (base, env, paths, transport, workers, ui,
  telemetry, integrations, dashboard, operator, service modules);
  `code_tools_service` 2217 -> 169 (its dispatcher is now per-operation handler
  modules); `app_core`, `bhm_mcp`, `memory_repository`, `repository_index`,
  `bhm_crystallize_worker`, `projection_reconciliation`, `change_impact`, `data_hygiene`,
  `source_registry`, `governed_consolidation` and many more are split the same way.
  Only three modules still exceed 1000 lines (`api_models`, `app_retrieval`, `app.py`).
- Public names of every split module are still importable from the original module
  (explicit re-exports); MCP tool catalog (37 tools), the route table (283 routes) and the
  OpenAPI contract are unchanged.
- The module-size ratchet baseline was lowered accordingly, so modules cannot grow back.
- No API, data-contract or storage changes. SQLite stays authoritative.

## Validation

- full gate: 3195 passed, 3 skipped; Ruff clean;
- the split of the code-tools dispatcher was reviewed independently by two agents
  against the previous version (24 operations, no differences found);
- release built from a clean worktree; launcher imports and opens from the frozen build.
