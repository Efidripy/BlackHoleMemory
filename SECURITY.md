# Security Policy

## Supported versions

Security fixes are maintained for the current release line declared by
`config/version-manifest.json`. The repository currently identifies itself as
`1.8.1`; the historical `v1.8.0` tag remains immutable. A future release must
use a new SemVer value and update the manifest atomically.

| Release line | Support |
| --- | --- |
| Current manifest release | Supported |
| Older release lines | Best effort only |
| Unreleased local builds | Not supported |

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Send a private
report to the repository owner through the private security-advisory channel
configured for the GitHub repository. Include:

- affected commit, tag, or local build receipt;
- a concise description and security impact;
- a minimal, non-destructive reproduction in an isolated authorized test
  environment;
- logs or screenshots with tokens, credentials, personal data, and runtime
  databases removed;
- any suggested mitigation and whether the issue is already known to be
  exploitable.

The maintainer will acknowledge receipt within 5 business days, provide a
triage decision or status update within 10 business days, and coordinate a
fix, disclosure date, and credit with the reporter. These targets are goals,
not a promise of acceptance or a guarantee of a public release.

## Security boundaries

- Never include `BHM_CALLER_TOKEN`, signing keys, private URLs, or SQLite/Qdrant
  data in an issue, pull request, CI log, or audit artifact.
- Release signatures, signer trust, source provenance, and post-install
  verification are separate gates; a mathematically valid signature alone is
  not sufficient release evidence.
- Production deployment, runtime data mutation, and external publication are
  operator-controlled actions outside the normal code-review scope.

## Disclosure and updates

Accepted issues are fixed on the canonical branch first, then backported only
when the maintainer can preserve the same security invariant. The changelog or
release notes will describe the impact and remediation without publishing
secrets or weaponized exploit details.
