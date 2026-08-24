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

## External recovery anchor and staged deletion

When reclaiming a local runtime that contains old migration receipts, first
create a portable online backup on an independent volume:

```powershell
uv run python .\scripts\bhm-create-external-live-backup.py `
  --destination F:\BHM-Safe-Backups\BlackHoleMemory\<UTC_TIMESTAMP>
```

The command verifies `memories.sqlite3`, `observations.sqlite3` and
`hook-jobs.sqlite3`, then writes an integrity manifest. Local
`.runtime/backups/` is staging only: a static dated path is never a permanent
recovery contract. If an operator explicitly stages historical data below
`.runtime/TEMP_TRASH/historical-prune-*`, delete it only after API, SQLite and
Qdrant smoke checks and through the digest-gated finalizer:

```powershell
uv run python .\scripts\bhm-finalize-runtime-temp-trash.py `
  --stage historical-prune-<UTC_TIMESTAMP>
```

Use the returned `plan_digest` only after reviewing the plan, then pass it with
`--apply --confirm-plan-digest`. The finalizer rejects reparse points, a stale
plan, and an external backup that does not verify every active SQLite database.

The inventory reports bytes per governed root. Some roots intentionally
overlap (for example, `.runtime/backups` and a named backup subdirectory), so
`summary.reported_item_bytes` is a diagnostic total and is not a disk-usage
total.

While BHM is running, SQLite `-wal` and `-shm` sidecars can disappear between
directory enumeration and metadata collection. The inventory ignores only this
transient disappearance; access errors, a missing governed root and every
reparse point remain visible as non-success states.

The checked-in policy tracks the current runtime: active SQLite, active Qdrant
and the still-retained WL-174 validation receipt. Historical paths are added
only while they exist. Age alone never proves that a payload is disposable; an
explicit operator decision with a verified external recovery anchor is required
before staging and finalizing deletion.
