from datetime import datetime, timezone

from blackholememory.factory_integration import build_factory_integration_preview
from blackholememory.factory_integration import verify_factory_integration_digest


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _fixture():
    artifacts = [{"id": "failure-1", "kind": "test", "path": "tests/test_graph.py", "status": "failure", "content": "assertion failed", "severity": "high"}, {"id": "log-1", "kind": "incident", "path": "ops/incident.log", "status": "failure", "content": "same failure", "severity": "medium"}]
    documents = [{"path": "README.md", "content": "# README\nSee [missing](references/missing.md)\n"}, {"path": "references/architecture/0001.md", "content": "# Context\n# Decision\n"}]
    code_items = [{"path": "src/graph.py", "symbol": "build_graph", "kind": "function", "test_paths": ["tests/test_graph.py"], "source_ref": "src/graph.py#build_graph"}]
    task_items = [{"task_id": "task-1", "status": "open", "files_touched": ["src/graph.py"], "evidence_refs": ["tests/test_graph.py"], "source_ref": "task:task-1"}]
    return artifacts, documents, code_items, task_items


def test_factory_integration_crosswalks_evidence_and_stays_proposal_only():
    artifacts, documents, code_items, task_items = _fixture()
    preview = build_factory_integration_preview(artifacts, documents, project="fixture", changed_paths=["src/graph.py"], code_items=code_items, task_items=task_items, risk_class="high", max_items=16, now=NOW)
    assert verify_factory_integration_digest(preview)
    assert preview["execution"]["tests_started"] is False
    assert preview["execution"]["documents_written"] is False
    assert preview["gates"]["code_impact_is_not_coverage"] is True
    assert preview["crosswalk"][0]["test_refs"] == ["tests/test_graph.py"]
    assert preview["documentation"]["summary"]["broken_link_count"] == 1
    assert preview["review_queue"]
    assert all(item["requires_human_review"] for item in preview["review_queue"])


def test_factory_integration_digest_is_stable():
    artifacts, documents, code_items, task_items = _fixture()
    first = build_factory_integration_preview(artifacts, documents, project="fixture", changed_paths=["src/graph.py"], code_items=code_items, task_items=task_items, now=NOW)
    second = build_factory_integration_preview(artifacts, documents, project="fixture", changed_paths=["src/graph.py"], code_items=code_items, task_items=task_items, now=NOW)
    assert first["preview_digest"] == second["preview_digest"]
