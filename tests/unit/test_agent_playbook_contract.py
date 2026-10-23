from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

import blackholememory.agent_playbook_contract as contract
from blackholememory import bhm_mcp
from blackholememory.agent_playbook_contract import (
    AgentTaskKind,
    PlaybookLanguage,
    build_agent_playbook,
    playbook_contract_digest,
)


def test_playbook_is_deterministic_content_free_and_read_only():
    first = build_agent_playbook(
        task_kind=AgentTaskKind.CODE_CHANGE,
        language=PlaybookLanguage.EN,
    )
    second = build_agent_playbook(
        task_kind="code_change",
        language="en",
    )

    assert first == second
    assert first["schema_version"] == "bhm.agent-playbook.v1"
    assert first["contract_digest"] == playbook_contract_digest()
    assert first["execution"] == {
        "writes_sqlite": False,
        "writes_qdrant": False,
        "writes_runtime": False,
        "writes_client_files": False,
    }
    assert [item["tool"] for item in first["recommended_read_only_tools"]] == [
        "bhm_change_impact_preview",
        "bhm_context_compile",
        "bhm_check_index_coverage",
    ]
    assert first["persistent_actions"]["default"] == "forbidden_without_explicit_authorization"


@pytest.mark.parametrize("task_kind", list(AgentTaskKind))
def test_each_task_kind_recommends_a_small_read_only_set(task_kind: AgentTaskKind):
    payload = build_agent_playbook(task_kind=task_kind)

    tools = payload["recommended_read_only_tools"]
    assert 2 <= len(tools) <= 4
    assert len({item["tool"] for item in tools}) == len(tools)
    assert all(item["tool"].startswith("bhm_") for item in tools)


def test_russian_playbook_preserves_authority_and_fallback_rules():
    payload = build_agent_playbook(task_kind="handoff", language="ru")

    assert payload["language"] == "ru"
    assert payload["native_attach_rule"] == {
        "catalog_primary": True,
        "health_once_if_attached": True,
        "no_self_repair": True,
        "steps": payload["native_attach_rule"]["steps"],
    }
    assert "Git" in payload["freshness_boundary"]
    assert "BHM evidence" in payload["fallback_when_mcp_unavailable"][0]
    assert payload["persistent_actions"]["requires"] == [
        "explicit Task Brief",
        "owner authorization",
    ]


def test_invalid_task_kind_or_language_fails_closed():
    with pytest.raises(ValueError):
        build_agent_playbook(task_kind="delete_everything")
    with pytest.raises(ValueError):
        build_agent_playbook(language="de")


def test_pure_contract_has_no_runtime_storage_or_transport_imports():
    source_path = Path(inspect.getfile(contract))
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )

    assert imports.isdisjoint(
        {"sqlite3", "httpx", "requests", "fastapi", "fastmcp", "pathlib", "os"}
    )



def test_playbook_is_registered_in_the_default_core_catalog():
    async def _names() -> list[str]:
        return [tool.name for tool in await bhm_mcp.mcp.list_tools()]

    names = asyncio.run(_names())

    assert names.count("bhm_agent_playbook") == 1
    assert bhm_mcp.bhm_agent_playbook("code_change:ru")["language"] == "ru"
