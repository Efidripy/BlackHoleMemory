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
  -> governed proposal queue and explicit operator review
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
- The model does not select or reproduce opaque memory/revision IDs. For every
  non-`no_op` proposal it selects bounded one-based `evidence_refs` among the
  model-visible records; trusted code converts them to exact same-project
  canonical IDs, revisions and content hashes. Missing, duplicate,
  out-of-range or ambiguous references become `no_op`.
- `revise`, `supersede`, `archive` and `link` require exactly one
  `target_evidence_ref`; BHM binds it to the candidate target. `link` requires
  exactly two bound endpoints. The policy receipt digest, normalized candidate,
  support rows and target/source bindings are canonical-JSON SHA-256 values
  inside the proposal identity. A changed basis, candidate, receipt or target
  fails validation before storage, decision or apply.
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
- Any declared conflict produces `no_op`, including `link`; no conflicted
  semantic lifecycle suggestion reaches the queue. Low-confidence
  `create`/`revise` also becomes `no_op`.
- A one-record historical rollover is permitted only when every selected basis
  row is an explicitly typed historical trace. A mixed historical/current basis
  is an ordinary consolidation: it cannot bypass duplicate or independent
  evidence checks, and it may retire only the exact typed trace rows after
  those checks pass.
- A model-generated `no_op` keeps only its bounded reason; its candidate is
  replaced with the canonical empty no-op candidate before any operator view.
- Default mode keeps all semantic lifecycle operations manual. A semantic
  policy receipt must have the current schema/version and consistent
  `model_operation` / `effective_operation` fields before it can be reviewed;
  absent, stale or inconsistent receipts fail closed. The current receipt has
  an immutable record-level support and target map, but it does not prove that
  the model's language is semantically entailed by those records. A
  local-model semantic proposal is therefore never automatically approved or
  applied.
  `BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED` remains a configured policy signal,
  while status reports `automatic_apply_enabled=false` and its reason. This
  keeps configuration visibility without representing an operator-only semantic
  lifecycle change as automatic.
- `BHM_GOVERNED_OPERATOR_CONSENT_REQUIRED=1` continues to take priority and is
  reported as `operator-consent`. Every semantic proposal still requires the
  existing privileged decision and exact-ID apply route after operator review.
  A semantic `approve` must carry the current operator-attestation schema in
  the approval event: exact proposal ID/digest, immutable evidence binding,
  model and effective operations, explicit target endpoint IDs (an explicit
  empty list for `create`), reviewer outcome `approved`, and a content-free
  semantic-review verdict bound to every support row. The verdict must state
  `candidate_entailment=supported`,
  `relevance_assessment=all_support_relevant`, and
  `hidden_conflict_assessment=none_observed`; any conflict, insufficiency,
  missing support row, legacy v1 attestation, or mismatch fails closed before
  lifecycle or outbox work. This is an accountable human review assertion,
  not an automatic proof of semantic truth.
  Non-semantic governed proposals retain their existing decision contract.
- The editor never calls `Mem0.add/update/delete`, writes Qdrant, starts a
  worker or polls a provider. Stored semantic proposals never auto-apply from
  their request route; a later operator-approved apply uses the existing
  SQLite-authoritative transaction and outbox path.

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
[Environment]::SetEnvironmentVariable('BHM_GOVERNED_OPERATOR_CONSENT_REQUIRED', '1', 'User')
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
   review queue. It writes a proposal row only; it never invokes semantic
   automatic review or lifecycle apply from this caller-scoped request.
3. In manual mode, inspect/validate the policy receipt and every bound support
   row. An authorized reviewer supplies the exact semantic attestation together
   with `approve`, explicitly attesting that the candidate is supported, every
   support row is relevant, and no hidden conflict was observed. It is recorded
   only in the proposal approval event JSON. Reject remains available without
   an attestation.
4. Re-read the proposal and run explicit apply through the existing privileged
   governed route or MCP tool with an exact proposal confirmation. Apply
   independently rechecks both SQLite revisions and the approval event binding.
   A future automatic mode requires a separately accepted semantic-entailment/
   conflict contract and end-to-end operator evidence.

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
approve/apply a proposal. Its result has only case IDs, raw model and effective
policy operations, boolean checks and redacted failure classes. It reports
`raw_model_operation_accuracy` separately from
`effective_policy_operation_accuracy`, plus policy-correction and false
positive/negative conflict counts. The compatibility field
`operation_accuracy` is explicitly the effective-policy metric, never LLM
accuracy.

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

`store_proposal=true` creates reviewable proposals and metrics only.
`GET /bhm/governed-consolidation/semantic-shadow-metrics` reports counts and
outcomes. Model confidence is never truth by itself: the current policy gates
and SQLite revalidation still require an operator for semantic lifecycle
changes.
