# BHM authority and projection boundaries

BlackHoleMemory has one authoritative lifecycle/metadata writer: the
SQLite-authoritative service. Qdrant and Mem0 are downstream, rebuildable
projection/semantic layers; they are not alternate writers for canonical
memory state.

## JSON sidecars

JSON sidecars used by checkpoint, session, task, link, handoff and related
compatibility paths are bounded artifacts/read models. They must not be used as
an independent source of truth or edited to change canonical lifecycle state.
Canonical writes go through the SQLite repository/service transaction and its
outbox boundary. A sidecar may be regenerated, compared, exported or retained
for compatibility, but a sidecar-only update is not a valid BHM state change.

The legacy semantic dependency graph is one such read model. Use
`scripts/plan-bhm-semantic-graph-migration.py` only to produce a bounded,
content-free, read-only classification against SQLite endpoint state and the
explicitly active project ontology. Its output is never an apply plan:
unregistered relations, missing/inactive endpoints and cross-project edges are
not remapped or copied automatically. A later migration requires the exact
same snapshot, a verified backup, a typed dry-run, explicit operator approval
and post-apply parity smoke.

## Hierarchical context tiers

The opt-in `tiered_context=true` compiler is a read-only context-selection
policy, not a memory lifecycle controller. Its `working`, `session`,
`project` and `archival` budgets emit deterministic inclusion/omission and
provenance receipts; lifecycle anchors explicitly declare `promotion=none`.
Run `scripts/validate-bhm-context-tiers.py` only against its synthetic offline
fixture to check this contract. It does not read live memories or mutate
SQLite, Qdrant, Mem0, a promotion lock or a session record. Any future
session-to-durable promotion remains an independently approved, typed
operation.

## Projection safety

- Read/search routes (`/bhm/search`, `/bhm/search/advanced`,
  `/bhm/context/compile` and `/bhm/retrieval/explain`) return a `side_effects`
  object with `read_only=true`, `sqlite_mutation=false`,
  `qdrant_mutation=false` and `projection_mutation=false`.
- `POST /bhm/memory/used` is the separate explicit access-feedback operation;
  its `side_effects.projection_update` is `explicit-access-feedback` and its
  response reports whether a bounded Qdrant payload update was scheduled.
- Qdrant reconciliation is deterministic and rollback-aware; it consumes
  SQLite state and never promotes an orphan vector to authority.
- Every canonical memory projection carries a stable
  `projection_payload_digest` over non-volatile SQLite metadata. A digest
  mismatch is projection drift. Metadata-only drift is repaired with bounded
  `set_payload`; embedding/upsert is reserved for vector-text changes or legacy
  points that do not yet carry the digest.
- Refinery apply uses one `BEGIN IMMEDIATE` transaction, validates the complete
  authoritative memory snapshot before writing, preserves storage tombstones,
  and updates project columns in memories, links and artifacts together. Alias
  collisions fail closed; derived memory/task graphs are rebuilt afterwards.
- Provenance is inferred only from positive source evidence. An unresolved
  origin remains unresolved and is reported in plan quality statistics; it is
  never relabelled `synthetic` by default.
- The launcher-managed projection sidecar may claim and acknowledge only
  transactional outbox leases in `sqlite-shadow` mode; it is not an
  authoritative memory writer and must not be folded into the API process.
- Authoritative runtime readiness requires SQLite schema/parity checks, while
  Qdrant degradation is reported separately.
- The normal Windows authoritative launcher sets
  `BHM_QDRANT_REQUIRED_FOR_CORE=false`: Docker/Qdrant outages leave the
  SQLite API and MCP ready, while detailed health reports a degraded
  projection and SLO remains breached. Strict deployment profiles retain the
  fail-closed default by leaving the flag enabled.
- Projection infrastructure failures return claimed outbox rows to `pending`
  without consuming retry attempts; payload/schema failures remain bounded
  and may enter dead-letter.

When a compatibility path cannot yet be migrated, its receipt must identify the
source (`sqlite-authoritative` or `read-model`), the authority boundary and
whether any write occurred. This prevents a JSON artifact from silently
reintroducing a second authority.
