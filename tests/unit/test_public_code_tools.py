from __future__ import annotations

import asyncio

from pathlib import Path

import pytest

from blackholememory import bhm_mcp, bhm_mcp_code
from blackholememory.app import PublicCodeToolRequest
from blackholememory.code_tools_service import (
    _PUBLIC_CODE_TOOL_OPERATIONS,
    _resolve_public_code_root,
)


def test_public_code_tool_contract_is_allowlisted_and_read_only_by_default() -> None:
    request = PublicCodeToolRequest(operation="schema")
    assert request.apply is False
    assert request.root is None
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

    monkeypatch.setattr(bhm_mcp_code, "_public_code_tool", fake_public_code_tool)
    result = bhm_mcp.bhm_index_repository(
        project="demo",
        root="<WORKSPACE_ROOT>/repos/demo",
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
        "root": "<WORKSPACE_ROOT>/repos/demo",
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


def test_mcp_index_rejects_slice_above_limit_with_actionable_error(monkeypatch) -> None:
    monkeypatch.setattr(bhm_mcp_code, "_public_code_tool", lambda *args, **kwargs: pytest.fail("must not call API"))
    with pytest.raises(ValueError, match="between 1 and 666, got 965"):
        bhm_mcp.bhm_index_repository(apply=True, max_files_per_run=965)


def test_mcp_status_preserves_omitted_root_for_fail_closed_resolution(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_public_code_tool(operation: str, **kwargs):
        captured["operation"] = operation
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(bhm_mcp_code, "_public_code_tool", fake_public_code_tool)

    assert bhm_mcp.bhm_index_status(project="demo") == {"ok": True}
    assert captured == {"operation": "status", "project": "demo", "root": None}


def test_public_code_coverage_semantics_with_policy_and_unexpected_skips(monkeypatch, tmp_path: Path) -> None:
    from blackholememory.code_tools_service_graph import _op_coverage
    from blackholememory.api_models import PublicCodeToolRequest

    # (a) Mock current snapshot with only policy skips
    mock_policy_summary = {
        "file_count": 10,
        "repository_file_count": 12,
        "relevant_skipped_count": 2,
        "relevant_skip_reasons": {"secret-content": 1, "unsupported-type": 1},
        "parse_status": {"parsed": 10},
        "parser_error_count": 0,
    }
    
    async def mock_resolve_scope(req, http_req=None):
        return (tmp_path / "db.sqlite3", None, "demo", tmp_path, "root_1")

    async def mock_run_read(name, func, *args, **kwargs):
        if name == "code.graph.current":
            return {"graph_snapshot_id": "g-1", "summary": dict(mock_policy_summary)}
        if name == "code.coverage.index":
            return {"fresh": True}
        return await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else func(*args, **kwargs)

    monkeypatch.setattr("blackholememory.code_tools_service_base._resolve_code_tool_scope", mock_resolve_scope)
    monkeypatch.setattr("blackholememory.code_tools_service_graph._run_bounded_read", mock_run_read)

    req = PublicCodeToolRequest(operation="coverage", project="demo", root=str(tmp_path))
    res_policy = asyncio.run(_op_coverage(req))
    assert res_policy["coverage"]["complete"] is True
    assert res_policy["coverage"]["policy_skipped_count"] == 2
    assert res_policy["coverage"]["policy_skip_reasons"] == {"secret-content": 1, "unsupported-type": 1}
    assert res_policy["summary"]["index_coverage_complete"] is True
    assert res_policy["summary"]["policy_skipped_count"] == 2

    # (b) Mock current snapshot with unexpected skip (oversized)
    mock_unexpected_summary = {
        "file_count": 10,
        "repository_file_count": 11,
        "relevant_skipped_count": 1,
        "relevant_skip_reasons": {"oversized": 1},
        "parse_status": {"parsed": 10},
        "parser_error_count": 0,
    }

    async def mock_run_read_unexpected(name, func, *args, **kwargs):
        if name == "code.graph.current":
            return {"graph_snapshot_id": "g-2", "summary": dict(mock_unexpected_summary)}
        if name == "code.coverage.index":
            return {"fresh": True}
        return await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else func(*args, **kwargs)

    monkeypatch.setattr("blackholememory.code_tools_service_graph._run_bounded_read", mock_run_read_unexpected)
    res_unexpected = asyncio.run(_op_coverage(req))
    assert res_unexpected["coverage"]["complete"] is False
    assert res_unexpected["coverage"]["policy_skipped_count"] == 0
    assert res_unexpected["summary"]["index_coverage_complete"] is False
