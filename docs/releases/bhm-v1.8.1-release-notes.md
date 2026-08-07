# BlackHoleMemory v1.8.1 — remediation release identity

Статус: release-candidate preparation. Исторический tag `v1.8.0` immutable и
не перемещается.

## Identity

- release version: `1.8.1`;
- channel: `PURE`;
- runtime: `bhm-v1.8.1-PURE`;
- broker: `ipc-broker-v1.8.1`;
- UI: `Runtime v1.8.1-PURE`;
- plugin: `1.8.1`.

## Included remediation

- project/root caller binding for scoped `/bhm/code-tools` requests;
- centralized MCP JSON-RPC secret redaction;
- explicit security policy and caller-token runbook;
- CI quality/public-boundary/acceptance gates;
- version marker and Dockerfile parser contract corrections.

## Release blockers

This document does not claim a signed or publishable release. The following
gates remain mandatory before packaging:

- exact clean tracked-tree build;
- pinned signer trust and detached-signature verification;
- canonical provenance, SBOM, build-inputs and LICENSE binding;
- post-install and rollback receipts;
- UI bootstrap host-user/bearer boundary;
- operator approval for external signing and publication.
