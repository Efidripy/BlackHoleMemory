from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.app import PublicCodeToolRequest
from blackholememory.app import _PUBLIC_CODE_TOOL_OPERATIONS
from blackholememory.app import _resolve_public_code_root


def test_public_code_tool_contract_is_allowlisted_and_read_only_by_default() -> None:
    request = PublicCodeToolRequest(operation="schema")
    assert request.apply is False
    assert request.operation in _PUBLIC_CODE_TOOL_OPERATIONS
    assert PublicCodeToolRequest(operation="graph", graph_operation="resolve").graph_operation == "resolve"
    with pytest.raises(ValueError):
        PublicCodeToolRequest(operation="cypher")


def test_public_code_root_rejects_escape_and_quarantine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repos = tmp_path / "repos"
    project = repos / "demo"
    project.mkdir(parents=True)
    monkeypatch.setattr("blackholememory.app.settings.repo_root", project)

    assert _resolve_public_code_root(str(project)) == project.resolve()
    with pytest.raises(Exception):
        _resolve_public_code_root(str(tmp_path / "outside"))

def test_public_code_tool_force_refresh_is_explicit() -> None:
    request = PublicCodeToolRequest(operation="index", apply=True, force_refresh=True)
    assert request.force_refresh is True
