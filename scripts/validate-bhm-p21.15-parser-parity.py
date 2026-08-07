#!/usr/bin/env python3
"""Measure the current bounded parser registry and graph parity fixtures."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from blackholememory.code_graph import PARSER_REGISTRY, PARSER_REGISTRY_DIGEST, extract_code_graph
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.repository_index import _language_for_path


FIXTURES = {
    "python": "from pathlib import Path\nclass Child:\n    pass\ndef route(value):\n    return helper(value)\n",
    "javascript": 'import x from "x";\nclass Child extends Base {}\nfunction route(req) { return helper(req); }\n',
    "typescript": 'import type { X } from "x";\nclass Child implements X {}\nexport function route(req: X): X { return helper(req); }\n',
    "powershell": "function Invoke-Route { Invoke-Helper }\nfunction Invoke-Helper { return $true }\n",
    "markdown": "# Architecture\n\n## Routes\n\n- `GET /health`\n",
}


def _write_report(path: Path, report: dict[str, Any]) -> None:
    replace_bytes_safely(path, (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _workspace_language_counts(repo: Path) -> dict[str, int]:
    ignored = {".git", ".src", ".venv", "venv", "node_modules", "__pycache__"}
    counts: Counter[str] = Counter()
    for path in repo.rglob("*"):
        if not path.is_file() or any(part in ignored for part in path.parts):
            continue
        language = _language_for_path(path.relative_to(repo).as_posix())
        if language in PARSER_REGISTRY:
            counts[language] += 1
    return dict(sorted(counts.items()))


def _fixture_snapshot(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for language, content in FIXTURES.items():
        suffix = {"python": ".py", "javascript": ".js", "typescript": ".ts", "powershell": ".ps1", "markdown": ".md"}[language]
        path = root / f"fixture{suffix}"
        payload = content.encode("utf-8")
        path.write_bytes(payload)
        files.append({"path": path.name, "language": language, "file_kind": "source", "size_bytes": len(payload), "content_sha256": _sha256(payload), "origin": "p21.15-fixture"})
    return {"root_id": "p21_15_fixture", "root_path": str(root), "project": "p21.15", "snapshot_id": "fixture-snapshot", "snapshot_digest": "fixture-snapshot-digest", "graph_input_digest": "fixture-input-digest", "files": files}


def validate(repo: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="bhm-p21-15-", dir=repo) as raw:
        root = Path(raw)
        snapshot = _fixture_snapshot(root)
        first = extract_code_graph(snapshot)
        second = extract_code_graph(snapshot)
    statuses = {item["language"]: item["status"] for item in first["parse_results"]}
    errors = {item["language"]: item["error_code"] for item in first["parse_results"] if item["status"] == "error"}
    return {
        "schema_version": "bhm.p21.15.wi33.parser-parity.v1",
        "generated_at": "2026-07-21",
        "plan_id": "BHM-V5-POST-ACCEPTANCE-20260717",
        "workspace_language_counts": _workspace_language_counts(repo),
        "parser_registry": PARSER_REGISTRY,
        "parser_registry_digest": PARSER_REGISTRY_DIGEST,
        "external_grammar_additions": [],
        "fixture_languages": sorted(FIXTURES),
        "fixture_statuses": statuses,
        "fixture_errors": errors,
        "graph_digest_first": first["graph_digest"],
        "graph_digest_second": second["graph_digest"],
        "stable_graph_digest": first["graph_digest"] == second["graph_digest"],
        "parser_error_count": first["summary"]["parser_error_count"],
        "ok": not errors and first["graph_digest"] == second["graph_digest"],
        "writes_live_state": False,
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = validate(args.repo_root.resolve())
    _write_report(args.report, report)
    print(json.dumps({"ok": report["ok"], "languages": report["fixture_languages"], "errors": report["fixture_errors"], "stable": report["stable_graph_digest"]}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
