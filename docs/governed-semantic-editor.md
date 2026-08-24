# Governed semantic editor

The governed semantic editor turns a bounded set of project-scoped retrieval
candidates into a proposal for operator review. It is a local-model capability,
not a second memory authority.

```text
Qdrant/Mem0 retrieval candidate IDs
  -> re-read current SQLite memories and revisions
  -> local model, strict JSON only
  -> deterministic policy validation
  -> governed proposal queue
  -> operator approve/reject, dry-run, explicit apply
  -> SQLite transaction -> outbox -> existing Qdrant projector
```

## Non-negotiable boundaries

- SQLite remains the only authoritative lifecycle and provenance store.
- Retrieval results are hints only. Every model input and proposal basis is
  re-read from the same project in SQLite before analysis.
- The local editor sees every candidate identity and revision, but evidence
  text has a fixed 12,000-character total budget. Full canonical revisions
  remain in SQLite for validation and operator review.
- If the bounded embedding contour is temporarily unavailable, one explicit
  request may use a bounded same-project SQLite lexical fallback. Its response
  reports `source=sqlite_lexical_fallback`; it is never represented as vector
  retrieval and returns an explicit retryable error when no lexical evidence
  matches.
- The editor can return only `no_op`, `create`, `revise`, `supersede`,
  `archive`, or `link` proposal types.
- Conflicts produce `no_op` (or an explicitly reviewed `link`), never a
  lifecycle suggestion. Low-confidence `create`/`revise` also becomes `no_op`.
- `archive` and `supersede` are always human-approved and still need the usual
  exact-ID `apply=true` confirmation.
- The editor never calls `Mem0.add/update/delete`, writes Qdrant, starts a
  worker, polls a provider, or applies a proposal itself.

## Activation

The base governed-consolidation schema and runtime flag must already be active.
The local model adapter has a second default-off flag:

```powershell
[Environment]::SetEnvironmentVariable('BHM_GOVERNED_SEMANTIC_EDITOR_ENABLED', '1', 'User')
[Environment]::SetEnvironmentVariable('BHM_GOVERNED_SEMANTIC_EDITOR_BASE_URL', 'http://127.0.0.1:1234/v1', 'User')
[Environment]::SetEnvironmentVariable('BHM_GOVERNED_SEMANTIC_EDITOR_MODEL', 'qwen2.5-coder-7b-instruct', 'User')
```

Restart the BHM API through the canonical launcher after changing persistent
environment variables. The canonical launcher imports this explicit,
non-secret governed allowlist from Windows User scope into its child process,
so the approved local configuration survives a desktop restart. The adapter accepts only the existing local-only
gateway boundary. It has a 45-second default timeout and a 900-token bound;
override only through the documented `BHM_GOVERNED_SEMANTIC_EDITOR_*` settings.

## Operator flow

1. Call `POST /bhm/governed-consolidation/semantic-proposals` with `project`,
   `query`, and `store_proposal=false` to inspect a non-persisted candidate.
2. Repeat with `store_proposal=true` only when the proposal belongs in the
   review queue. This writes a proposal row only; it does not mutate memory.
3. Inspect, validate, then approve or reject it through the existing governed
   routes or MCP tools.
4. Run dry-run. Only an explicit `apply=true` and matching proposal ID can
   make a canonical SQLite change.

The MCP equivalents are `bhm_governed_semantic_proposal` and
`bhm_governed_semantic_shadow_metrics`. They remain operator-scoped, like the
other governed tools.

## Quality gates

`tests/fixtures/governed-semantic-editor-golden.json` is a 30-case redacted
Multisubgen-style acceptance corpus. It has expected operations, required
conflicts, and prohibited operations, but no live memory text. The evaluator
checks a proposal factory without executing a model or writing any storage.

In shadow mode, `store_proposal=true` creates reviewable proposals and metrics
only. `GET /bhm/governed-consolidation/semantic-shadow-metrics` reports counts
and operator outcomes. It reports quality as unknown until explicit apply/reject
labels exist; model confidence is not treated as truth.
