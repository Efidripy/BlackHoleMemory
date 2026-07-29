"""Read-only structural acceptance report for the P28 CBM crosswalk."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable


ALLOWED_STATUSES = {"implemented", "equivalent", "partial", "deferred", "rejected", "not-applicable"}
CLOSING_STATUSES = {"implemented", "equivalent", "rejected", "not-applicable"}
_BLOCKED_EVIDENCE_PARTS = {".env", "credentials", "private-keys", "private_keys", "secrets"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_crosswalk_shape(root: Path, capabilities: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Check deterministic, repository-local evidence references.

    Acceptance evidence is a source-of-truth document, not a path traversal
    or secret-discovery mechanism.  Keep this gate read-only and fail closed
    for duplicate IDs, malformed records, absolute/parent paths, quarantine
    source references and symlinked evidence.
    """

    failures: list[str] = []
    seen: set[str] = set()
    checked = 0
    safe = 0
    for index, capability in enumerate(capabilities):
        identifier = str(capability.get("id") or f"capability[{index}]")
        if identifier in seen:
            failures.append(f"{identifier}: duplicate capability id")
        seen.add(identifier)
        if not str(capability.get("name") or "").strip():
            failures.append(f"{identifier}: missing name")
        evidence = capability.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            failures.append(f"{identifier}: evidence must be a non-empty list")
            continue
        for raw_path in evidence:
            checked += 1
            value = str(raw_path or "").replace("\\", "/").strip()
            path = Path(value)
            parts = {part.casefold() for part in path.parts}
            if not value or path.is_absolute() or ".." in path.parts:
                failures.append(f"{identifier}: unsafe evidence path {value!r}")
                continue
            quarantine_manifest = ".src" in parts and path.name.casefold() == "source-manifest.json"
            if (".src" in parts and not quarantine_manifest) or parts & _BLOCKED_EVIDENCE_PARTS:
                failures.append(f"{identifier}: evidence path crosses blocked boundary {value!r}")
                continue
            candidate = root / path
            evidence_path = candidate.resolve()
            try:
                evidence_path.relative_to(root)
            except ValueError:
                failures.append(f"{identifier}: evidence path escapes repository {value!r}")
                continue
            if not candidate.is_file() or candidate.is_symlink() or not evidence_path.is_file():
                failures.append(f"{identifier}: evidence is not a regular file {value!r}")
                continue
            safe += 1
    return {"checked": checked, "safe": safe, "failures": failures}


def build_report(repo_root: Path) -> dict[str, Any]:
    root = repo_root.resolve()
    crosswalk_path = root / ".docs" / "config" / "cbm-bhm-capability-crosswalk.json"
    crosswalk = json.loads(crosswalk_path.read_text(encoding="utf-8"))
    capabilities = list(crosswalk.get("capabilities") or [])
    active_capabilities = [
        capability for capability in capabilities if str(capability.get("status") or "") != "not-applicable"
    ]
    acceptance = crosswalk.get("acceptance") if isinstance(crosswalk.get("acceptance"), dict) else {}
    bounded_disposition = acceptance.get("bounded_disposition") if isinstance(acceptance.get("bounded_disposition"), dict) else {}
    bounded_disposition = {
        identifier: value
        for identifier, value in bounded_disposition.items()
        if any(str(capability.get("id") or "") == identifier for capability in active_capabilities)
    }
    failures: list[str] = []
    open_capabilities: list[str] = []
    checked_evidence = 0
    for capability in active_capabilities:
        identifier = str(capability.get("id") or "unknown")
        status = str(capability.get("status") or "")
        if status not in ALLOWED_STATUSES:
            failures.append(f"{identifier}: invalid status {status!r}")
        if status not in CLOSING_STATUSES:
            open_capabilities.append(identifier)
        for evidence in capability.get("evidence") or []:
            checked_evidence += 1
            evidence_path = root / str(evidence)
            if not evidence_path.is_file():
                failures.append(f"{identifier}: missing evidence {evidence}")
    for evidence in acceptance.get("evidence") or []:
        checked_evidence += 1
        evidence_path = root / str(evidence)
        if not evidence_path.is_file():
            failures.append(f"acceptance: missing evidence {evidence}")
    shape = _validate_crosswalk_shape(root, active_capabilities)
    failures.extend(shape["failures"])
    try:
        tracked_src = subprocess.run(
            ["git", "-C", str(root), "ls-files", ".src"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        tracked_src = ["git-check-unavailable"]
        failures.append("git source-boundary check unavailable")
    bounded_scope_closed = all(
        str(bounded_disposition.get(str(capability.get("id") or "")) or "").strip()
        for capability in active_capabilities
    )
    local_open_capabilities = list(open_capabilities)
    operator_gated_capabilities = [
        identifier
        for identifier in local_open_capabilities
        if str(bounded_disposition.get(identifier) or "").startswith("operator-gated")
    ]
    local_product_ready = not failures and bounded_scope_closed and not tracked_src
    return {
        "schema_version": "bhm.p28.acceptance-report.v1",
        "ok": not failures,
        "acceptance_ready": local_product_ready,
        "acceptance_semantics": "local_product",
        "local_product_ready": local_product_ready,
        "crosswalk_sha256": _sha256(crosswalk_path),
        "capability_count": len(active_capabilities),
        "checked_evidence_count": checked_evidence,
        "evidence_boundary": {"checked": shape["checked"], "safe": shape["safe"], "clean": not shape["failures"]},
        "open_capabilities": open_capabilities,
        "local_open_capabilities": local_open_capabilities,
        "operator_gated_capabilities": operator_gated_capabilities,
        "bounded_disposition": bounded_disposition,
        "bounded_scope_closed": bounded_scope_closed,
        "failures": failures,
        "source_boundary": {"tracked_src_entries": tracked_src, "clean": not tracked_src},
        "execution": {"writes_sqlite_state": False, "writes_qdrant": False, "writes_worktree": False, "raw_source_returned": False},
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(args.repo)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
