# BlackHoleMemory v1.8.1 — remediation release

Статус: source release-ready. Исторический tag `v1.8.0` immutable и не
перемещается. Operator signing, packaging и GitHub publication выполняются
отдельными release-операциями и не считаются завершёнными до появления их
проверяемых артефактов.

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
- version marker and Dockerfile parser contract corrections;
- hermetic public CI and isolated admin-auth integration coverage;
- authenticated portable-install smoke with bounded cleanup and actionable
  startup diagnostics;
- descriptor-based bounded reads with symlink, reparse, hardlink, race and
  byte-limit rejection;
- confined admin snapshot import/export and canonical MCP repair cleanup;
- server-inventory-only repository intelligence path selection;
- fixed allowlist of public Workbench error codes.

## Validation

- BHM CI passed on commit `a5ae072754ebe7f8375860348ad305ad81911351`;
- CodeQL completed successfully on the same commit;
- open GitHub code-scanning, Dependabot and secret-scanning alerts: `0`;
- release fixture, version manifest, public tree, documentation links,
  workflow pinning, resource limits, auth/admin parity, REST/MCP parity and
  search parity gates passed;
- workspace mixed-agent gate: `157 PASS`, `0 WARN`, `0 FAIL`.

## Publication gates

This source state is eligible for packaging. A published release still
requires all of the following receipts:

- exact clean tracked-tree build;
- pinned signer trust and detached-signature verification;
- canonical provenance, SBOM, build-inputs and LICENSE binding;
- post-install and rollback receipts;
- UI bootstrap host-user/bearer boundary;
- explicit operator-signed publication with
  `independent_external=false`.
