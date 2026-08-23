# Local artifact cleanup

`bhm-local-artifact-cleanup.py` removes only explicitly listed, regenerable
local artifacts. It is separate from SQLite retention and data hygiene: it
never touches authoritative memory, Qdrant data, backups, rollback receipts,
source quarantine, release assets, or operator evidence.

The checked-in policy is intentionally narrow. Each rule matches only direct
children of an allowlisted root; junctions/symlinks are rejected. The default
mode is read-only and produces an immutable candidate set with `plan_digest`.

```powershell
$asOf = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$plan = uv run python .\scripts\bhm-local-artifact-cleanup.py --as-of $asOf | ConvertFrom-Json
$plan.summary

uv run python .\scripts\bhm-local-artifact-cleanup.py `
  --apply `
  --as-of $asOf `
  --confirm-plan-digest $plan.plan_digest
```

If the policy, candidate set, metadata, or timestamp changes between dry-run
and apply, the digest mismatch fails before deletion. Treat an unexpected
candidate as a policy bug: do not apply it; adjust or remove the rule and add a
regression test first.

An unreadable candidate is emitted in `blocked`; an apply with any blocked
entry is refused. Do not weaken ACLs during routine cleanup. Record the residue
and resolve its ownership separately.

The policy lives in
[`config/local-artifact-retention-policy.json`](../config/local-artifact-retention-policy.json).
It currently permits only stale root test/lint/browser caches, stale local
coverage/log files, empty historical `pytest-*` runtime and runtime-legacy scratch directories,
and one named superseded launcher cold-start rehearsal whose root launcher and
release archive were independently verified afterward. New high-volume runtime
categories must be classified by owner, recovery dependency and retention
period before they gain a rule.
