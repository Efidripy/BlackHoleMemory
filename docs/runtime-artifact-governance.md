# Runtime artifact governance

Local runtime data has three distinct lifecycles. Ignored status is not a
deletion permission.

| Disposition | Meaning | Allowed action |
| --- | --- | --- |
| `protected` | Active authority, active projection or current recovery anchor | No archive/delete through cleanup tooling. |
| `manual-classification` | Mixed lineage where a child may be active or historical | Classify the exact child and its recovery dependency first. |
| `archive-review` | Historical proof that may be considered after its retention window | Produce an inventory, preserve receipt lineage, then obtain a separate archive decision. |

The policy is [`config/runtime-artifact-governance.json`](../config/runtime-artifact-governance.json).
Its companion command is read-only:

```powershell
uv run python .\scripts\bhm-runtime-artifact-inventory.py `
  --as-of 2026-08-24T00:00:00Z
```

`archive-review-due` does **not** authorize a move, compression or deletion.
Before any archive action, record the owner, source receipt, exact path,
size, digest/integrity evidence, destination, retention deadline and rollback
method. A current SQLite backup or a live authority/projection root is never a
candidate for this flow.

The inventory reports bytes per governed root. Some roots intentionally
overlap (for example, `.runtime/backups` and a named backup subdirectory), so
`summary.reported_item_bytes` is a diagnostic total and is not a disk-usage
total.

The initial policy keeps WL-292 refinery evidence, WL-174 validation evidence,
pre-reindex rollback copies and early migration/retention/reconciliation
backups for at least 90 days after their last change. This removes ambiguity
without pretending that age alone proves the data is disposable.
