# BlackHoleMemory v2.1.0 — "Paid Tier"

## Release identity

- release version: `2.1.0`; channel: `PURE`; runtime: `bhm-v2.1.0-PURE`; broker: `ipc-broker-v2.1.0`;
  UI: `Runtime v2.1.0-PURE`; plugin: `2.1.0`.

## Included changes

- **Paid LLM tier (opt-in).** The launcher panel has a "Paid model" switch (`llm.tier`, `BHM_LLM_TIER=paid`). In
  local mode nothing changes. In paid mode all chat consumers use the paid model through the local CPA proxy:
  hook fact synthesis, provider warmup, Mem0 LLM, search rerank (extra opt-in `BHM_PAID_RERANK=1`), doc2query
  enrichment, the governed semantic editor and the developer agent. Embeddings always stay on the local model.
  The tier is an explicit setting and is never inferred from a URL.
- **Egress filter.** Everything sent to the paid model passes `egress_filter`: secrets, emails, phones, cards (Luhn),
  IBAN, national ids, public IPv4, and now names, addresses and birth dates (by label and by shape). The gateway
  applies it itself for paid models (`ModelDefinition.egress_filter`) and drops Qwen-only request fields.
  Limit: free-form personal names without a label or patronymic are not detected (no NER).
- **Paid-call counters.** `paid_llm_calls` in the LLM telemetry (calls, errors, latency per site) when the tier is paid.
- **Alias retrieval lane (off by default).** `BHM_ALIAS_RETRIEVAL=1` plus the paid tier adds a table of paraphrased
  search questions and an extra retrieval contour. SQLite revisions and the Qdrant projector are untouched.
- Security worker and gate are unchanged and stay local-only; they are not part of the runtime path.
- No change to the MCP catalog, API contracts or storage. SQLite stays authoritative.

## Validation

- unit suite: 3062 passed; 3 tests already failed on the previous release (module-size ratchet for four modules,
  public script manifest, resource callsite inventory).
- live retrieval on the paid model: golden set pass@1 85.1%, pass@3 96.0%, refusals 100%; fresh-lessons
  paraphrase set varies by one or two queries between runs because the paid rerank is not deterministic.
