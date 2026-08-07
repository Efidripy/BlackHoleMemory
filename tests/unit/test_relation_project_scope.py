from __future__ import annotations

from blackholememory import app as bhm_app


def _memory(source_id: str, project: str, *, files: list[str] | None = None, tags: list[str] | None = None) -> dict:
    return {
        "source_id": source_id,
        "project": project,
        "memory_type": "knowledge",
        "content": f"memory {source_id}",
        "tags": tags or [],
        "metadata": {
            "raw_title": f"memory {source_id}",
            "files": files or [],
            "source_refs": [f"{source_id}.md"],
        },
    }


def test_aggregate_relation_suggestions_never_cross_projects(monkeypatch) -> None:
    monkeypatch.setattr(
        bhm_app,
        "_load_live_memories",
        lambda: [
            _memory("a", "blackholememory", files=["shared.py"]),
            _memory("b", "e-github-workspace", files=["shared.py"]),
        ],
    )

    result = bhm_app._relation_suggest(bhm_app.RelationSuggestRequest(project=None, limit=20))

    assert result["suggestions"] == []


def test_relation_apply_canonicalizes_alias_before_persisting(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(
        bhm_app,
        "_relation_suggest",
        lambda _request: {
            "suggestions": [
                {
                    "relation": "relates_to",
                    "score": 0.7,
                    "source_id": "a",
                    "target_id": "b",
                    "reason": "shared_files:x.py",
                }
            ]
        },
    )
    monkeypatch.setattr(
        bhm_app,
        "_find_live_memory",
        lambda memory_id, project=None: _memory(memory_id, "blackholememory"),
    )

    def capture(request):
        captured.append(request)
        return {"id": "link-1", "project": request.project}

    monkeypatch.setattr(bhm_app, "_create_memory_link", capture)

    result = bhm_app._relation_apply_suggestions(
        bhm_app.RelationApplySuggestionsRequest(
            project="BlackHoleMemory",
            min_score=0.6,
            limit=20,
            include_relates_to=True,
        )
    )

    assert result["count"] == 1
    assert captured[0].project == "blackholememory"
