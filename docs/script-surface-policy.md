# Script surface policy

`scripts/` is a public source surface. The exact registry in
[`config/public-script-manifest.json`](../config/public-script-manifest.json)
classifies every tracked script and separately says whether it belongs to an
installed launcher payload. An unlisted tracked script fails the public-tree
gate; an entry with `release: false` remains available to contributors and CI,
but is fail-closed out of the release payload.

The three supported boundaries are:

| Boundary | Contents | Audience |
| --- | --- | --- |
| Public source | Stable runtime/operator scripts plus reproducible tests, validators and frozen benchmark harnesses. | Contributors, CI and auditors. |
| Packaged release | Only manifest entries with `release: true`: launcher/runtime dependencies and supported operator/recovery controls. | Operator and launcher user. |
| `.local/` | Machine-bound, credential-bound, historical or project-hardcoded drills with an integrity manifest. | This development checkout only. |

Tests and public quality gates are source evidence, not end-user launcher
features. Their visibility allows independent review and reproducible CI; the
release flag prevents them from inflating the installed application.

## Naming

Public script names use lowercase kebab-case and describe their action:

```text
bhm-<verb>-<object>[-<qualifier>].py
<verb>-bhm-<object>[-<qualifier>].ps1
```

Allowed verbs are `start`, `stop`, `run`, `check`, `validate`,
`audit`, `build`, `verify`, `repair`, `migrate`, `plan`,
`apply`, `manage`, `export` and `restore`. Work-item, worklist, phase,
incident and personal identifiers do not belong in a public filename.

The sole naming exception is a manifest entry with role `runtime-support` that
is imported as a Python module by the launcher or another public runtime
entrypoint. It may keep its `snake_case` module name until its implementation
is moved into `src/blackholememory/`; renaming that kind of file mechanically
would break imports and packaged-launcher contracts. Such a module is not an
operator CLI and must not acquire a second public wrapper merely to satisfy
the filename convention.

## Local-only tooling

One-shot research, synthetic acceptance drills, historical migration receipts,
machine-bound diagnostics, benchmark experiments and credential-bound helpers
belong below `.local/scripts/`. That directory is intentionally ignored and is
never included in a release. Historical identifiers may remain there because
they are local provenance, not public operator contracts.

Before moving a script out of the public surface, prove that it is not required
by the package, release materialization, CI, public documentation or a
publicly tracked test. Move its matching local-only test and fixture in the
same change when needed.

## Release manifest

Each public script has exactly one manifest entry with its canonical path,
role and boolean `release` eligibility. The top-level `release_roles` profile
is the second fail-closed decision: only eligible entries in this explicit
role set are shipped beside the launcher. The current profile includes
runtime, runtime-support and supported operator/recovery roles; it excludes
`benchmark`, `quality-gate` and `release`, which remain source/CI tooling.
The manifest is part of the tracked source snapshot and is checked by
public-tree, materialization and staged-source verification. Adding a script
to `scripts/` does not implicitly package it: the change must add a reviewed
entry and explicitly choose its release eligibility and role. Removing or
relocating a script must remove the matching entry in the same commit.

## Migration rule

Rename and relocate one role group at a time. Update code, tests, CI, docs,
release packaging and any launch specification atomically; then run the
public-tree validator, affected tests and a runtime smoke check. Compatibility
wrappers are temporary and must be tracked in the migration receipt rather
than becoming a second permanent entrypoint.
