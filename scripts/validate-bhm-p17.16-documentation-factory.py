"""Deterministic offline gate for the P17.16 Documentation/Ops/Vision Factory."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from blackholememory.documentation_factory import DOCUMENTATION_FACTORY_SCHEMA_VERSION
from blackholememory.documentation_factory import build_documentation_factory_preview
from blackholememory.documentation_factory import verify_documentation_factory_digest


def main() -> int:
    documents = [
        {"path": "README.md", "content": "# Project\nSee [missing](docs/missing.md).\n"},
        {"path": ".docs/adr/0121-demo.md", "content": "# Status\nAccepted\n# Decision\nBounded.\n"},
    ]
    preview = build_documentation_factory_preview(
        documents,
        project="blackholememory",
        vision_assets=[{"path": "screens/home.png"}],
        vision_confirmed=False,
        now=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    checks = {
        "schema": preview["schema_version"] == DOCUMENTATION_FACTORY_SCHEMA_VERSION,
        "digest": verify_documentation_factory_digest(preview),
        "findings": bool(preview["findings"]),
        "patches": bool(preview["patches"]),
        "link_gate": preview["gates"]["link_gate"] is False,
        "vision_fail_closed": preview["vision"]["status"] == "disabled_unconfirmed_capability",
        "vision_execution_off": preview["execution"]["ocr_started"] is False,
        "proposal_only": preview["execution"]["documents_written"] is False and preview["execution"]["git_started"] is False,
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
