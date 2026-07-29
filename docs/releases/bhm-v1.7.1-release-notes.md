# BlackHoleMemory v1.7.1

## Release state

`prepared-not-published`

The v1.7.1 release bundle was prepared and verified locally. This document
records the release boundary; it does not authorize external publication.

sha256: 9d53110925c395d74d8c1d39d7bd4ad7ea3a79ecee242ea12ade98912319e07c

## Verification

- SQLite remains the authoritative lifecycle and metadata store.
- Qdrant is a rebuildable projection and passed the scoped native projection
  parity checks.
- Release build, trust metadata, archive safety and post-install checks were
  verified locally.
- The release surface excludes local-only development material and source
  quarantine content.

## Publication boundary

Tag, push и внешняя публикация не выполнялись.

External publication requires an explicit operator decision, a final release
identity and the corresponding signature/provenance evidence. Preparing or
verifying a local bundle does not perform those actions implicitly.
