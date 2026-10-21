# BlackHoleMemory v1.8.8 — new Galaxy: the memory Atlas

## Release identity

- release version: `1.8.8`;
- channel: `PURE`;
- runtime: `bhm-v1.8.8-PURE`;
- broker: `ipc-broker-v1.8.8`;
- UI: `Runtime v1.8.8-PURE`;
- plugin: `1.8.8`.

## Included changes

- **`/bhm/galaxy` is now the Atlas.** One dense, glowing 3D map of every active
  memory, laid out from its real embedding (cosine kNN, a minimum-spanning-tree
  skeleton and the persisted memory links). Memories are coloured by project
  (or class, or freshness), grouped around one "mother" node per project, with
  mixed colour where projects intersect. Free 3D rotation, travelling pulses on
  links, search, a created-date timeline with playback, and "tug" dragging with
  viscous graded follow: a mother pulls her nearest links, bigger records pull
  more of their project, small ones less.
- **New read endpoint `GET /bhm/galaxy/atlas`** (operator with all projects only,
  UI-session allowed). The layout is built in a background thread from Qdrant
  vectors and cached under `<runtime>/galaxy/atlas-cache.json`; the cache is
  rebuilt at most every 10 minutes after the memory store changes. While the first
  build runs the page shows a progress note.
- **Removed:** the G6 `/bhm/atlas` page, the view selector and the bundled G6
  libraries. The previous viewer stays at `/bhm/galaxy/classic` and opens inside
  the Atlas "Инструменты" drawer.
- Read-only: SQLite stays authoritative, nothing is written to memory or Qdrant.

## Validation

- new unit tests (`tests/unit/test_galaxy_atlas.py`) and updated route/launcher tests;
- full gate: see the release commit.
