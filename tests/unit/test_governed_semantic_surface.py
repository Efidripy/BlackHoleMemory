from __future__ import annotations

import asyncio

from blackholememory.domain import Memory
from blackholememory import app as bhm_app


def _record(memory_id: str, content: str) -> dict:
    return Memory.from_record(
        {
            "source_system": "bhm",
            "source_id": memory_id,
            "project": "multiserversubgen",
            "memory_type": "decision",
            "content": content,
            "created_at": "2026-08-24T10:00:00Z",
            "updated_at": "2026-08-24T10:00:00Z",
            "metadata": {"raw_title": memory_id},
        }
    ).to_record()


class _MemoryService:
    def __init__(self, records: list[dict]) -> None:
        self.records = records

    def get_records(self, memory_ids: list[str], *, project: str) -> list[dict]:
        assert project == "multiserversubgen"
        return [item for item in self.records if item["source_id"] in memory_ids]


class _Admission:
    allowed = True
    code = "admitted"

    def as_dict(self) -> dict:
        return {"allowed": True, "code": "admitted"}


class _Governor:
    def __init__(self) -> None:
        self.released: list[str] = []

    def admit(self, _request):
        return _Admission()

    def release(self, job_id: str) -> bool:
        self.released.append(job_id)
        return True


class _Completion:
    def __init__(self, _config) -> None:
        pass

    def complete(self, *, project: str, query: str, records: list[dict]) -> dict:
        assert project == "multiserversubgen"
        assert query == "uninstall safety"
        return {
            "operation": "create",
            "basis_memory_ids": [item["source_id"] for item in records],
            "candidate": {
                "title": "Installer safety",
                "content": "Normal uninstall stays project-scoped and requires an install-state log.",
                "memory_type": "decision",
                "concepts": ["uninstall"],
                "files": [],
            },
            "confidence": 0.9,
            "conflicts": [],
            "reason": "same project facts agree",
        }


def test_semantic_surface_retrieves_projection_candidates_then_revalidates_sqlite(monkeypatch) -> None:
    records = [_record("mem_bhm_a", "Normal uninstall is project-scoped."), _record("mem_bhm_b", "Install-state log is required.")]
    governor = _Governor()

    async def _search(query: str, project: str, limit: int):
        assert (query, project, limit) == ("uninstall safety", "multiserversubgen", 12)
        return ([{"metadata": {"source_id": "mem_bhm_b"}}, {"metadata": {"source_id": "mem_bhm_a"}}], 2)

    monkeypatch.setenv("BHM_GOVERNED_SEMANTIC_EDITOR_ENABLED", "1")
    monkeypatch.setattr(bhm_app, "_require_governed_consolidation_enabled", lambda: None)
    monkeypatch.setattr(bhm_app, "_governed_consolidation_project", lambda _principal, project: project)
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: _MemoryService(records))
    monkeypatch.setattr(bhm_app, "federated_search", _search)
    monkeypatch.setattr(bhm_app, "LocalGatewaySemanticCompletion", _Completion)
    monkeypatch.setattr(bhm_app, "_llm_governor", lambda: governor)

    result = asyncio.run(
        bhm_app._governed_semantic_proposal(
            bhm_app.GovernedSemanticProposalRequest(project="multiserversubgen", query="uninstall safety"),
            principal=object(),
        )
    )

    assert result["stored"] is False
    assert result["proposal"]["operation"] == "create"
    assert result["retrieval"] == {
        "source": "federated_semantic_candidates",
        "candidate_count": 2,
        "sqlite_revalidated_count": 2,
    }
    assert result["side_effects"]["memory_lifecycle_mutation"] is False
    assert result["side_effects"]["qdrant_mutation"] is False
    assert governor.released
