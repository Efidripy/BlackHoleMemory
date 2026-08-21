# Agent and model routing

BHM uses a capability- and evidence-based routing policy. The default rule is
to use the minimum sufficient local tier for a task, then escalate only when
the requested capability, measured context profile, risk, or mutation boundary
requires it.

## Routing order

1. `light` — bounded classification, retrieval, documentation drafts, code
   indexing and test selection when the local capability contract is enough.
2. `standard` — broader local work when the light tier cannot satisfy the
   requested capability or context profile.
3. `deep` — explicitly justified high-context or high-reasoning work.
4. `Codex`/operator review — architecture, security, release, destructive or
   final integration work, or any route that fails the local evidence gates.

The model router selects by capability and measured context first, then by
`selection_tier`, latency and stable model id. A faster deep model therefore
does not displace a slower light model when both satisfy the same contract.

## Safety boundaries

- Routing is local-first and fail-closed; cloud fallback is disabled.
- Route endpoints are proposal/decision surfaces. They do not start an agent,
  apply a patch or mutate authoritative memory automatically.
- SQLite remains the authoritative memory store. Routing metadata must not be
  treated as memory content or written into Mem0/Qdrant as a substitute for
  provenance.
- Architecture, security, release, destructive and final-integration work
  keeps its stronger review/integrator requirements even when a local model is
  available.

The active implementation is in `src/blackholememory/model_router.py`,
`src/blackholememory/capability_router.py` and
`src/blackholememory/llm_delegation_policy.py`. The local operational backlog
is `.docs/TODO.md`; completed work is indexed by `.docs/DONE.md`. Both are
intentionally ignored by the public tree and are the only active BHM status
files (ADR-0622).

