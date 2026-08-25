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
  -> manual review/apply OR default-off deterministic auto review/apply
  -> SQLite transaction -> outbox -> existing Qdrant projector
```

## Non-negotiable boundaries

- SQLite remains the only authoritative lifecycle and provenance store.
- Retrieval results are hints only. Every model input and proposal basis is
  re-read from the same project in SQLite before analysis.
- The local editor sees a bounded semantic view of each candidate, with a fixed
  6,000-character total evidence budget. Full canonical identities and
  revisions remain in SQLite for validation and operator review.
- The model-facing proposal contract is BHM-authored `system` text. Retrieved
  evidence is sent separately as untrusted `user` data; BHM does not rely on
  provider-specific `developer` roles that Qwen-compatible templates may
  reject. A rejected syntactic or semantic response gets at most three fresh
  strict-contract attempts, then becomes an explicit deterministic `no_op`.
- When a local OpenAI-compatible runner supports `response_format=json_schema`,
  the editor supplies its bounded proposal schema. BHM still independently
  parses every response, re-reads its SQLite basis and rejects invalid content.
- The model does not select or reproduce opaque memory/revision IDs. BHM binds
  every proposal to its deterministic, already SQLite-revalidated bounded
  evidence basis before it constructs the external proposal.
- If the local model itself is unavailable, BHM emits an explicitly labelled
  deterministic `no_op` preview instead of treating the failure as a memory
  write or a semantic result. The receipt reports a stable redacted
  `model_fallback_reason` (for example `schema_validation_failed` or
  `transport_error`), never raw provider text.
- If the bounded embedding contour is temporarily unavailable, one explicit
  request may use a bounded same-project SQLite lexical fallback. Its response
  reports `source=sqlite_lexical_fallback`; it is never represented as vector
  retrieval and returns an explicit retryable error when no lexical evidence
  matches.
- The editor can return only `no_op`, `create`, `revise`, `supersede`,
  `archive`, or `link` proposal types.
- Conflicts produce `no_op` (or an explicitly reviewed `link`), never a
  lifecycle suggestion. Low-confidence `create`/`revise` also becomes `no_op`.
- A model-generated `no_op` keeps only its bounded reason; its candidate is
  replaced with the canonical empty no-op candidate before any operator view.
- Default mode keeps all operations manual. The separately default-off
  `BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED` policy can decide and apply a stored
  local-model proposal without the two manual actions. Its thresholds are
  `0.90` for `create`/`revise`/`link` and `0.97` for `supersede`/`archive`; all
  conflicts, `no_op`, fallback, missing editor receipt and low-confidence cases
  are rejected. The event receipt contains only policy version, actor digest
  and reason codes.
- The editor never calls `Mem0.add/update/delete`, writes Qdrant, starts a
  worker or polls a provider. Automatic apply, when opted in, is performed by a
  separate deterministic policy adapter through the existing SQLite transaction.

## Activation

The base governed-consolidation schema and runtime flag must already be active.
The local model adapter has a second default-off flag:

```powershell
[Environment]::SetEnvironmentVariable('BHM_GOVERNED_SEMANTIC_EDITOR_ENABLED', '1', 'User')
[Environment]::SetEnvironmentVariable('BHM_GOVERNED_SEMANTIC_EDITOR_BASE_URL', 'http://127.0.0.1:1234/v1', 'User')
[Environment]::SetEnvironmentVariable('BHM_GOVERNED_SEMANTIC_EDITOR_MODEL', 'qwen2.5-coder-7b-instruct', 'User')
```

The automatic policy stage is independent and remains off unless explicitly
enabled after its disposable rehearsal:

```powershell
[Environment]::SetEnvironmentVariable('BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED', '1', 'User')
```

Restart the BHM API through the canonical launcher after changing persistent
environment variables. The canonical launcher imports this explicit,
non-secret governed allowlist from Windows User scope into its child process,
so the approved local configuration survives a desktop restart. The adapter accepts only the existing local-only
gateway boundary. It has a 60-second default timeout and a 180-token compact
proposal bound; override only through the documented
`BHM_GOVERNED_SEMANTIC_EDITOR_*` settings. The default is deliberately not a
long prose budget: that would make a healthy local 7B provider look unavailable
before it could finish a foreground proposal.

## Operator flow

1. Call `POST /bhm/governed-consolidation/semantic-proposals` with `project`,
   `query`, and `store_proposal=false` to inspect a non-persisted candidate.
2. Repeat with `store_proposal=true` only when the proposal belongs in the
   review queue. With auto-review disabled, this writes a proposal row only.
3. In manual mode, inspect/validate, decide and run explicit apply through the
   existing governed routes or MCP tools.
4. In auto mode, the persisted local-model proposal is reviewed immediately by
   the deterministic policy and, if eligible, passed to the same transactional
   apply. The response returns a content-safe policy/apply receipt.

The MCP equivalents are `bhm_governed_semantic_proposal` and
`bhm_governed_semantic_shadow_metrics`. They remain operator-scoped, like the
other governed tools.

## Quality gates

`tests/fixtures/governed-semantic-editor-golden.json` is a 30-case redacted
Multisubgen-style acceptance corpus. It has expected operations, required
conflicts, and prohibited operations, but no live memory text. The evaluator
checks a proposal factory without executing a model or writing any storage.

That label-only corpus proves evaluator mechanics, not local-model usefulness.
`tests/fixtures/governed-semantic-editor-evidence.json` is the separate
30-case synthetic, redacted evidence corpus for a real proposal-only model
gate: 29 same-project model cases plus one cross-project fail-closed preflight.
It contains neither production memory, credentials, local paths, nor model
outputs. The gate cannot persist to SQLite or a queue, write Qdrant/Mem0, or
approve/apply a proposal. Its result has only case IDs, expected/actual
operations, boolean checks and redacted failure classes.

Run it only with an explicitly enabled local editor and write its receipt
under ignored runtime scratch space:

```powershell
$env:BHM_GOVERNED_SEMANTIC_EDITOR_ENABLED = '1'
$env:BHM_GOVERNED_SEMANTIC_EDITOR_MODEL = 'qwen/qwen2.5-coder-14b'
uv run python scripts/evaluate-governed-semantic-editor.py `
  --output .runtime/scratch/governed-semantic-model-evaluation.json
```

The gate is intentionally strict: zero forbidden operations, no authority
boundary violation, every conflict safely routed, each operation family
represented, cross-project evidence rejected before the model call, and at
least 90% exact post-policy operation accuracy. Candidate-term coverage applies
to content-producing `create`/`revise`/`supersede` operations; a `link` is
validated by its typed relation and same-project basis instead. It does not
weaken a failed threshold. A failed run is evidence to improve the
prompt/model/fixture, not permission to auto-apply or alter the live review
queue.

With auto-review disabled, `store_proposal=true` creates reviewable proposals
and metrics only. `GET /bhm/governed-consolidation/semantic-shadow-metrics`
reports counts and outcomes. Model confidence is never truth by itself: the
automatic mode adds deterministic policy gates and SQLite revalidation, rather
than asking the model to approve or apply its own output.
