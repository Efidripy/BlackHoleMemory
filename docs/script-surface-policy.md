# Script surface policy

`scripts/` is a public product surface. Release materialization consumes only
the explicit entries in
[`config/public-script-manifest.json`](../config/public-script-manifest.json);
an unlisted tracked script is fail-closed out of the release payload. It must
therefore contain only stable,
documented entrypoints for installation, runtime operation, recovery, release
build/verification, and hermetic public quality gates.

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
role, and `release: true`. The manifest is part of the tracked source
snapshot and is checked by both materialization and staged-source verification.
Adding a script to `scripts/` does not publish or package it: the change must
also add a reviewed manifest entry. Removing or relocating a script must remove
the matching entry in the same commit.

## Migration rule

Rename and relocate one role group at a time. Update code, tests, CI, docs,
release packaging and any launch specification atomically; then run the
public-tree validator, affected tests and a runtime smoke check. Compatibility
wrappers are temporary and must be tracked in the migration receipt rather
than becoming a second permanent entrypoint.
