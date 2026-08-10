from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory import bhm_mcp
from blackholememory.app import PublicCodeToolRequest
from blackholememory.app import _PUBLIC_CODE_TOOL_OPERATIONS
from blackholememory.app import _resolve_public_code_root


def test_public_code_tool_contract_is_allowlisted_and_read_only_by_default() -> None:
    request = PublicCodeToolRequest(operation="schema")
    assert request.apply is False
    assert request.operation in _PUBLIC_CODE_TOOL_OPERATIONS
    assert request.max_files_per_run == 666
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


def test_mcp_index_forwards_bounded_resume_and_graph_receipt_fields(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_public_code_tool(operation: str, **kwargs):
        captured["operation"] = operation
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(bhm_mcp, "_public_code_tool", fake_public_code_tool)
    result = bhm_mcp.bhm_index_repository(
        project="demo",
        root="E:/GitHub/repos/demo",
        apply=True,
        force_refresh=True,
        max_files_per_run=123,
        defer_graph=True,
        graph_only=False,
        snapshot_id="snapshot-1",
    )

    assert result == {"ok": True}
    assert captured == {
        "operation": "index",
        "project": "demo",
        "root": "E:/GitHub/repos/demo",
        "apply": True,
        "build_graph": True,
        "defer_graph": True,
        "graph_only": False,
        "force_refresh": True,
        "max_files_per_run": 123,
        "expected_job_id": None,
        "expected_state_digest": None,
        "snapshot_id": "snapshot-1",
    }
