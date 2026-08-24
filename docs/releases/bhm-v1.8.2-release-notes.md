# BlackHoleMemory v1.8.2 — release-surface cleanup

Статус: published-source release candidate. Tag `v1.8.1` остаётся immutable.

## Identity

- release version: `1.8.2`;
- channel: `PURE`;
- runtime: `bhm-v1.8.2-PURE`;
- broker: `ipc-broker-v1.8.2`;
- UI: `Runtime v1.8.2-PURE`;
- plugin: `1.8.2`.

## Included changes

- Один публичный реестр покрывает каждый tracked script; добавление
  неучтённого файла fail-closed.
- Standalone release verifier требует public script manifest и точного
  соответствия packaged `scripts/**` его allowlist.
- Launcher profile включает только runtime/runtime-support и supported
  operator/recovery roles: `87` scripts вместо `226` public source scripts.
- `139` benchmark, quality-gate и release-build scripts остаются в GitHub для
  review, CI и воспроизводимости, но не устанавливаются пользователю.
- Шесть machine-bound/historical/credential-bound helpers вынесены в ignored
  local archive с SHA-256 integrity entries; SQLite/Qdrant/runtime state не
  изменялись.

## Validation

- focused release/public-boundary suite: `41 passed`;
- ruff for affected scripts: passed;
- public-tree, release fixture and documentation-link gates: passed;
- actual release build and archive verification are recorded with the
  published release assets.

## Trust boundary

This release uses the existing operator-checksum trust mode unless a separate
operator signing key is supplied. It does not claim an external signature.
