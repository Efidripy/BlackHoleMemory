"""Deterministic offline gate for the P17.14 Repository Intelligence preview."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from blackholememory.repository_intelligence import REPOSITORY_INTELLIGENCE_SCHEMA_VERSION
from blackholememory.repository_intelligence import build_repository_intelligence_preview
from blackholememory.repository_intelligence import verify_repository_intelligence_digest


def main() -> int:
    files = [
        {"path": "src/pkg/core.py", "content": "from pkg.util import helper\n\n# TODO: split\ndef run(value):\n    return helper(value)\n"},
        {"path": "src/pkg/util.py", "content": "def helper(value):\n    return value\n"},
        {"path": "tests/test_core.py", "content": "from pkg.core import run\n\ndef test_run():\n    assert run(1)\n"},
    ]
    preview = build_repository_intelligence_preview(
        files,
        project="blackholememory",
        changed_paths=["src/pkg/util.py"],
        now=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    checks = {
        "schema": preview["schema_version"] == REPOSITORY_INTELLIGENCE_SCHEMA_VERSION,
        "digest": verify_repository_intelligence_digest(preview),
        "file_symbols": preview["summary"]["symbol_count"] >= 3,
        "architecture_map": bool(preview["architectural_map"]["nodes"]),
        "dependency_impact": preview["dependency_impact"]["status"] == "computed",
        "test_selection": preview["test_selection"]["status"] == "computed",
        "technical_debt": bool(preview["technical_debt"]),
        "issue_clusters": bool(preview["issue_clusters"]),
        "source_refs": all(item.get("source_ref") for item in preview["technical_debt"]),
        "proposal_only": preview["execution"]["model_started"] is False and preview["execution"]["writes_performed"] is False,
    }
    report = {
        "ok": all(checks.values()),
        "schema_version": preview["schema_version"],
        "preview_digest": preview["preview_digest"],
        "summary": preview["summary"],
        "checks": checks,
        "execution_enabled": False,
        "auto_apply": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
