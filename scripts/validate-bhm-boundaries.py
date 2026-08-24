#!/usr/bin/env python3
"""Record explicit dispositions for residual supply-chain, Windows and DR scope."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from blackholememory.filesystem_boundaries import replace_bytes_safely

ROOT = Path(__file__).resolve().parents[1]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _write_report(path: Path, report: dict) -> None:
    replace_bytes_safely(
        path,
        (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    registry = json.loads((ROOT / "config" / "source-registry.json").read_text(encoding="utf-8"))
    packages = lock.count("[[package]]")
    urls = len(re.findall(r"\burl\s*=\s*\"https?://", lock))
    hashes = len(re.findall(r"(?m)\bhash\s*=\s*\"sha256:", lock))
    sources = registry.get("sources", []) if isinstance(registry, dict) else []
    source_metadata = {
        "entries": len(sources),
        "written_permission": sum(item.get("permission_status") == "written-permission" for item in sources),
        "revision_present": sum(bool(item.get("revision")) for item in sources),
        "recheck_present": sum(bool(item.get("recheck_date")) for item in sources),
        "code_copy_allowed": sum(item.get("code_copy_allowed") is True for item in sources),
    }
    report = {
        "schema_version": "bhm.p21.7-9.boundary-dispositions.v1",
        "p21_7_supply_chain": {
            "disposition": "rejected_current_scope",
            "verified": {"uv_lock_packages": packages, "uv_lock_urls": urls, "uv_lock_hashes": hashes, "release_trust": True, "permission_registry": source_metadata},
            "unresolved": ["release signature not configured", "SBOM package-level lock hash/download bindings absent", "consolidated egress/dual-control receipt absent"],
            "risk": "operator-checksum trust is weaker than an independently configured signature and SBOM package binding",
            "decision": "ship only as prepared-not-published; require new ADR/Plan ID before signed publication or supply-chain expansion",
        },
        "p21_8_windows_corpus": {
            "disposition": "rejected_current_scope",
            "verified": ["portable install", "upgrade/rollback", "slash normalization", "symlink rejection", "basic CRLF/LF handling"],
            "unresolved": ["collision", "junction", "UNC/long path", "worktree/submodule/sparse", "dirty/rename", "monorepo", "restart contention"],
            "risk": "unmeasured Windows edge cases can regress repository indexing and rollback",
            "decision": "retain current compatibility boundary; do not claim exhaustive Windows corpus until a dedicated fixture matrix exists",
        },
        "p21_9_dr_operations": {
            "disposition": "rejected_current_scope",
            "verified": ["SQLite/WAL backup/restore", "Qdrant projection recovery", "Mem0 boundary", "MCP recovery", "migration rollback", "source registry metadata"],
            "unresolved": ["explicit RTO/RPO", "disk-full runbook", "Qwen outage/recovery", "source freshness/license recheck", "post-recovery digest checklist"],
            "risk": "operator recovery may be slower or less auditable than the desired SLO without these explicit procedures",
            "decision": "retain existing runbook and record the missing procedures as a new operations workstream; no hidden automation is enabled",
        },
        "checks": {
            "lockfile_pinned": packages >= 100 and urls > 0 and hashes > 0,
            "permission_metadata_complete": source_metadata["entries"] == source_metadata["written_permission"] == source_metadata["revision_present"] == source_metadata["recheck_present"],
            "code_copy_boundary": source_metadata["code_copy_allowed"] <= 1,
            "dispositions_explicit": True,
        },
        "rollback": "remove this disposition receipt; no runtime, package or source data mutation",
        "final_integrator": "codex:/root",
    }
    report["digest"] = _digest(report)
    report["ok"] = all(bool(value) for value in report["checks"].values())
    _write_report(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
