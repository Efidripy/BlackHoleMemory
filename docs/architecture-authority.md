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

`scripts/apply-bhm-semantic-graph-links.py` is the separate local operator
surface for that later step. Its default action is still `plan`; `apply`
accepts only a sealed content-free plan plus a distinct, integrity-checked
SQLite backup, `--confirm`, and an explicit `--maintenance-window-open`
writer-drain/offline proof. It binds the raw legacy JSON SHA-256, active
ontology registry and activation artifacts, SQLite endpoint/schema snapshot,
canonical project link set and unique candidate-key set. Inside one
`BEGIN IMMEDIATE` transaction it rechecks those bindings and inserts only
exact active same-project `DEPENDS_ON -> depends_on` relations directly into
`memory_links`. Existing canonical links with different provenance are kept;
existing exact migration rows are no-ops; any drift, natural-relation
ambiguity or deterministic id collision rolls back the whole batch. The tool
never rewrites the JSON graph and never writes Qdrant, Mem0, the outbox or a
memory lifecycle. It is deliberately not an API or MCP route.

`scripts/propose-bhm-legacy-ontology-schemas.py` can separately derive a
content-free, proposal-only per-project schema candidate from exact
same-project active `DEPENDS_ON` read-model edges. It neither persists nor
activates a schema; `UPGRADES` and every missing, inactive or cross-project
edge remain explicit review reasons. A proposal still requires operator review,
explicit persistence, explicit activation and an admission smoke before it can
govern a write.

## Governed shared-memory reads

The shared-memory policy surface is default-deny and SQLite-authoritative.
`POST /bhm/shared-memory/read` and the matching admin MCP tool are present
only as an opt-in bounded read contract: `BHM_SHARED_MEMORY_READ_ENABLED=1`
is required, a caller must be explicitly scoped to the project, and the
request owner must match the owner recorded on the active SQLite memory before
any grant is evaluated. One unambiguous active immutable grant must allow the
same project, owner, visibility and `read` operation; a denied attempt keeps a
content-free audit record but returns no memory data. The response is a bounded
canonical SQLite subset. Shared writes remain disabled, and this route never
reads/writes Qdrant or Mem0 or changes a memory lifecycle.

## Hierarchical context tiers

The opt-in `tiered_context=true` compiler is a read-only context-selection
policy, not a memory lifecycle controller. Its `working`, `session`,
`project` and `archival` budgets emit deterministic inclusion/omission and
provenance receipts; lifecycle anchors explicitly declare `promotion=none`.
Run `scripts/validate-bhm-context-tiers.py` only against its synthetic offline
fixture to check this contract. It does not read live memories or mutate
SQLite, Qdrant, Mem0, a promotion lock or a session record. Lifecycle receipts
also derive a content-free deterministic *lock preview* from the event and
source-reference digests, but it is always `not_acquired`, cannot reserve work
and cannot select a candidate. Any future session-to-durable promotion remains
an independently approved, typed operation that must bind a candidate digest,
SQLite snapshot, policy decision and transactional lease.

The compatibility MCP `bhm_observe` wrapper accepts the same optional
`parentEventId` used by the REST observation contract. Clients should provide
it for `resume` events when a `PreCompact` anchor exists; forwarding the link
only creates a content-free receipt and never enables promotion or durable
memory mutation.

## External evaluation datasets

LoCoMo and LongMemEval are never fetched or evaluated by default. A later
local smoke fixture must first pass
`scripts/validate-bhm-external-evaluation-dataset.py`: it binds a local file,
license-evidence file, pinned credential-free HTTPS source revision and
explicit local-evaluation-only review to SHA-256 digests. Its content-free
receipt does not load data into BHM or enable a ranker; model runs remain a
separate bounded and approved operation.

The BHM-owned recorded-receipt fixture separately reports category, session,
turn and route retrieval metrics plus temporal accuracy, update consistency,
abstention precision/recall, p50/p95 latency and project/provenance coverage.
Absent receipt scope or provenance is explicitly `unproven`; a mismatch is a
visible isolation failure, never a silently accepted score. These offline
metrics remain evaluation evidence and cannot enable a runtime retrieval path.

## Projection safety

- Read/search routes (`/bhm/search`, `/bhm/search/advanced`,
  `/bhm/context/compile` and `/bhm/retrieval/explain`) return a `side_effects`
  object with `read_only=true`, `sqlite_mutation=false`,
  `qdrant_mutation=false` and `projection_mutation=false`.
- Canonical `POST /bhm/search` additionally returns a content-free
  `bhm.retrieval-contour-trace.v1` query-plan stage for the completed
  embedding/local/global/exact contour timings. Query-embedding preparation
  and each vector contour have independent three-second response deadlines; a
  `timed_out` contour is visible in the trace and cannot block a completed
  sibling contour or the established fallback. An embedding deadline returns
  the same content-free explicit fallback-grace route. It never includes query
  text, IDs, memory content, paths, scores or provider errors, and it cannot
  change ranking or feature flags.
- Explicit fallback-grace responses expose only an allowlisted content-free
  `stage` (`embedding_preparation`, `retrieval_contour`, `provider_transport`,
  `provider_http` or `unknown`) plus the typed exception name; raw provider
  messages, paths and request data remain excluded.
- The opt-in exact-identifier lane begins with a bounded, project-scoped,
  active-lifecycle SQLite substring prefilter that returns IDs only. Those IDs
  are then hydrated from SQLite and rechecked by the Python exact-token and
  route filters. The prefilter may over-return but cannot itself authorize a
  result; it has no cache, schema migration, Qdrant/Mem0 dependency, or write
  path.
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
