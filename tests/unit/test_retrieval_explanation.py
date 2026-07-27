from __future__ import annotations

from blackholememory.retrieval_explanation import explain_retrieval_hit


def test_explain_retrieval_hit_returns_bounded_allowlisted_signals():
    result = explain_retrieval_hit(
        {
            "id": "mem-1",
            "content": "A long retrieval explanation body.",
            "score": 0.81,
            "context_origin": "GLOBAL",
            "metadata": {
                "source_id": "source-1",
                "project": "blackholememory",
                "raw_title": "Canonical decision",
                "semantic_rank": 1,
                "lexical_rank": 2,
                "graph_rank": 3,
                "mmr_rank": 1,
                "semantic_score": 0.9,
                "rrf_score": 0.04,
                "fusion_channels": ["semantic", "lexical", "graph"],
                "decay_score": 0.7,
                "decay_lambda_per_day": 0.04,
                "vector_scope": "local+global",
                "vector_targets": ["local", "global"],
                "vector_collections": ["local", "global"],
                "graph_metadata": {
                    "is_graph_expansion": True,
                    "extended_from": "source-root",
                    "link_type": "DEPENDS_ON",
                },
                "secret_like": "must not leak",
            },
        },
        rank=2,
    )

    assert result["rank"] == 2
    assert result["id"] == "source-1"
    assert result["project"] == "blackholememory"
    assert result["context_origin"] == "GLOBAL"
    assert result["channels"] == ["semantic", "lexical", "graph"]
    assert result["ranks"] == {"semantic_rank": 1, "lexical_rank": 2, "graph_rank": 3, "mmr_rank": 1}
    assert result["scores"]["final_score"] == 0.81
    assert "multi_channel_fusion" in result["reason_codes"]
    assert result["graph"]["link_type"] == "DEPENDS_ON"
    assert "secret_like" not in result


def test_explain_retrieval_hit_fails_closed_for_unknown_origin_and_unbounded_values():
    result = explain_retrieval_hit(
        {
            "id": "mem-2",
            "content": "x" * 500,
            "context_origin": "unexpected",
            "metadata": {
                "project": "blackholememory",
                "fusion_channels": ["x" * 500] * 20,
            },
        },
        rank=0,
    )

    assert result["rank"] == 1
    assert result["context_origin"] == "LOCAL"
    assert result["reason_codes"] == ["local_contour"]
    assert len(result["content_preview"]) <= 240
    assert len(result["channels"]) == 8
    assert len(result["channels"][0]) == 160
