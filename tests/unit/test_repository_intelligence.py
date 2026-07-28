from __future__ import annotations

from datetime import datetime, timezone

import pytest

from blackholememory.repository_intelligence import RepositoryIntelligenceError
from blackholememory.repository_intelligence import build_repository_intelligence_preview
from blackholememory.repository_intelligence import collect_repository_files
from blackholememory.repository_intelligence import verify_repository_intelligence_digest


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _files() -> list[dict[str, str]]:
    return [
        {
            "path": "src/pkg/core.py",
            "content": "from pkg.util import helper\n\ndef run(value):\n    # TODO: split this path\n    try:\n        if value:\n            return helper(value)\n    except Exception:\n        return None\n",
        },
        {"path": "src/pkg/util.py", "content": "def helper(value):\n    return value\n"},
        {"path": "tests/test_core.py", "content": "from pkg.core import run\n\ndef test_run():\n    assert run(1)\n"},
        {"path": "docs/architecture.md", "content": "# Architecture\n\n## Retrieval\n"},
    ]


def test_preview_builds_symbols_architecture_impact_and_source_refs():
    preview = build_repository_intelligence_preview(
        _files(),
        project="demo",
        changed_paths=["src/pkg/util.py"],
        now=NOW,
    )

    assert preview["schema_version"] == "bhm.llm.repository-intelligence.v1"
    assert preview["summary"]["file_count"] == 4
    assert preview["summary"]["symbol_count"] >= 4
    assert preview["architectural_map"]["edges"]
    assert preview["dependency_impact"]["status"] == "computed"
    assert any(item["path"] == "src/pkg/core.py" for item in preview["dependency_impact"]["impacted_paths"])
    assert preview["test_selection"]["selected"]
    assert preview["technical_debt"]
    assert all("source_ref" in issue and "content" not in issue for issue in preview["technical_debt"])
    assert preview["issue_clusters"]
    assert preview["execution"]["writes_performed"] is False
    assert verify_repository_intelligence_digest(preview) is True


def test_collect_repository_files_is_allowlisted_and_bounded(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    files = collect_repository_files(tmp_path)

    assert [item["path"] for item in files] == ["src/main.py"]
    with pytest.raises(RepositoryIntelligenceError):
        collect_repository_files(tmp_path, ["../outside.py"])


def test_issue_clusters_and_test_selection_are_not_requested_without_changes():
    preview = build_repository_intelligence_preview(_files(), project="demo", now=NOW)

    assert preview["dependency_impact"]["status"] == "not_requested"
    assert preview["test_selection"]["status"] == "not_requested"
    assert preview["test_selection"]["candidate_tests"]
    assert preview["gates"]["bounded"] is True


def test_limits_and_unsafe_paths_fail_closed():
    with pytest.raises(RepositoryIntelligenceError):
        build_repository_intelligence_preview([{"path": "../secret.py", "content": "x"}])
    with pytest.raises(RepositoryIntelligenceError):
        build_repository_intelligence_preview(_files(), max_files=0)
    with pytest.raises(RepositoryIntelligenceError):
        build_repository_intelligence_preview(_files() * 17)


def test_javascript_parser_skips_pathological_lines_before_regex_matching():
    long_line = "import " + ("x" * 20_000) + " from 'module'"
    preview = build_repository_intelligence_preview([{"path": "app.js", "content": long_line}])

    assert preview["summary"]["file_count"] == 1
    assert preview["summary"]["symbol_count"] == 0
