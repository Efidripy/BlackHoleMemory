# BlackHoleMemory v2.0.0 — "Monster Dismantled"

## Release identity

- release version: `2.0.0`;
- channel: `PURE`;
- runtime: `bhm-v2.0.0-PURE`;
- broker: `ipc-broker-v2.0.0`;
- UI: `Runtime v2.0.0-PURE`;
- plugin: `2.0.0`.

## Included changes

- **FIX-017: the god-modules are taken apart. No behaviour change.**
  - `app.py` shrinks from 22 378 to 1 019 lines (FastAPI app, middleware, lifespan and
    `register(app)` calls). The routes live in `memory_routes`, `llm_routes`, `galaxy_routes`,
    `ui_routes` and the `app_*` domain modules; shared runtime state is in `app_runtime`.
  - `code_tools_service`, `code_graph` and `agents/developer_agent` are split into per-operation
    handlers and packages; the longest function in `code_tools_service` is now 79 lines and the
    import cycles in `developer_agent` are gone.
  - The launcher is split into `bhm_launcher_{host,telemetry,integrations,dashboard}`
    (4 487 → 3 011 lines in `bhm_launcher.py`); split code reads launcher globals through a
    late-bound handle, so existing monkeypatch-style tests keep working.
- **Safety nets added:** a route-table snapshot test, `check-undefined-globals`, an import-cycle
  test and a module-size ratchet (modules may not grow back past their baseline).
- Two real latent bugs were caught by the new checks and fixed (`NameError` on a rarely taken
  `developer_agent` branch).
- No API, data-contract or storage changes. SQLite stays authoritative.

## Validation

- full gate: 3195 passed, 3 skipped; Ruff clean;
- trial release build from a clean tree: archive verified (SHA-256 match, launcher modules present,
  `BHM_Launcher.exe --check-dependencies` exits 0).
