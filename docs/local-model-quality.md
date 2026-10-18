# Local model quality gate

`scripts/run-bhm-local-llm-quality.py` evaluates only an already-loaded exact
local LM Studio model against a small frozen synthetic Russian fixture. It is a
quality gate for the bounded answer contract, not a runtime feature and not a
claim of general intelligence.

## Contract

- native loopback API only: `http://127.0.0.1:13666` (or exact `::1`/`localhost`);
- exact model and variant from the fixture must exist and have exactly one
  loaded instance; it must be the only loaded LLM;
- allowed routes are exactly `GET /api/v1/models` and `POST /api/v1/chat`;
- the evaluator never requests model load, unload, download, configuration,
  BHM tools, MCP, SQLite, Mem0 or Qdrant;
- each chat call is deterministic (`temperature=0`), has `reasoning=off`,
  `store=false` and a 20-token output cap;
- any reported model-load event, unknown/wrong model, malformed fixture,
  remote endpoint, timeout or canonical-answer miss fails the gate. Canonical
  comparison normalizes Unicode and letter case only; it never rewrites facts.

The synthetic fixture has separate `core` and independent `holdout` splits.
It checks direct facts, two-hop facts, abstention, response format and prompt
injection resistance. Its `<AUTHORITATIVE_FACTS>` are the only factual source;
`<UNTRUSTED_QUOTE>` must never be followed as instructions or treated as fact.

## Run

```powershell
uv run python scripts/run-bhm-local-llm-quality.py `
  --repeats 3 `
  --output .runtime/scratch/local-model-quality.json
```

The optional receipt contains only case IDs, pass/fail bits, model contract,
aggregate latency and digests. It intentionally omits prompts, facts, quotes,
answers, provider internals and credentials.

## Interpretation

`100%` means that this exact model/variant, prompt contract, fixture, local
endpoint and invocation parameters passed every frozen case. It does **not**
prove universal factual accuracy, unrestricted reasoning quality, semantic
entailment, relevance, absence of hidden conflict, production readiness or
permission for automatic semantic approve/apply. SQLite remains authoritative;
automatic semantic approve/apply remains `NO-GO`.
