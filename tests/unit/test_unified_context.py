from __future__ import annotations

from pathlib import Path

from blackholememory.code_graph import build_code_graph
from blackholememory.convention_memory import build_convention_memory
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state
from blackholememory.unified_context import build_unified_context_from_graph
from blackholememory.unified_context import compile_unified_context


def test_source_aware_interleave_is_bounded_and_deterministic() -> None:
    sources = {
        "memory": [{"id": "m1", "title": "Memory", "content": "memory fact", "source_refs": ["memory:m1"]}],
        "code": [{"id": "c1", "title": "Code", "content": "function: run @ src/run.py", "source_refs": ["src/run.py#L1"], "files": ["src/run.py"]}],
        "conventions": [{"id": "v1", "title": "Convention", "content": "Use snake_case", "status": "proposal", "source_refs": ["references/architecture/0001.md#L1"]}],
        "tasks": [{"id": "t1", "title": "Task", "content": "Close migration gate", "source_refs": ["task:t1"]}],
        "docs": [{"id": "d1", "title": "Docs", "content": "Runbook", "source_refs": ["docs/runbook.md#L1"]}],
        "ops": [{"id": "o1", "title": "Ops", "content": "SLO healthy", "source_refs": ["ops/slo.json"]}],
    }
    first = compile_unified_context(sources, project="demo", query="run", token_budget=400, max_items_per_source=4)
    second = compile_unified_context(sources, project="demo", query="run", token_budget=400, max_items_per_source=4)
    assert first["response_digest"] == second["response_digest"]
    assert first["sources"]["requested"] == {"code": 1, "conventions": 1, "tasks": 1, "docs": 1, "ops": 1, "memory": 1}
    assert first["sources"]["included"]
    assert first["provenance"]["evidence_coverage"]["ratio"] == 1.0
    assert first["execution"]["writes_sqlite_state"] is False
    assert first["execution"]["public_mcp_changed"] is False


def test_unified_graph_and_convention_channels_are_read_only(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "main.py").write_text("def run_value():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_main.py").write_text("from main import run_value\n\ndef test_run_value():\n    assert run_value() == 1\n", encoding="utf-8")
    (root / "docs" / "adr" / "0001.md").write_text("# ADR\n", encoding="utf-8")
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi08", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    graph = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    build_convention_memory(database, project="demo", root_id=state.root_id, graph_snapshot_id=graph["graph_snapshot_id"])
    result = build_unified_context_from_graph(database, project="demo", root_id=state.root_id, query="run_value", memory_items=[{"id": "m1", "content": "fact", "source_refs": ["memory:m1"]}], include_code=True, include_conventions=True, include_proposals=True, token_budget=600, limit=8)
    assert result["schema_version"] == "bhm.unified-context.v1"
    assert result["sources"]["requested"]["code"] >= 1
    assert result["sources"]["requested"]["conventions"] >= 1
    assert result["diagnostics"]["code"]["graph_digest"]
    assert result["execution"]["writes_sqlite_state"] is False
    assert result["execution"]["writes_qdrant"] is False
    assert result["execution"]["raw_source_returned"] is False
