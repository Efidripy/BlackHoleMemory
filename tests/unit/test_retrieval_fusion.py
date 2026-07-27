from __future__ import annotations

import pytest

from blackholememory.retrieval_fusion import rank_channel_from_scores
from blackholememory.retrieval_fusion import weighted_rank_fusion


def test_weighted_rank_fusion_combines_optional_graph_channel():
    scores = weighted_rank_fusion(
        {
            "semantic": {"vector": 1, "graph-only": 3},
            "lexical": {"vector": 1},
            "graph": {"graph-only": 1},
        },
        weights={"graph": 0.7},
    )

    assert scores["vector"] == pytest.approx(2 / 61)
    assert scores["graph-only"] == pytest.approx(1 / 63 + 0.7 / 61)
    assert scores["graph-only"] > 1 / 63


def test_rank_channel_from_scores_is_deterministic_for_ties():
    assert rank_channel_from_scores({"b": 1.0, "a": 1.0, "c": 0.5}) == {"a": 1, "b": 2, "c": 3}


def test_weighted_rank_fusion_rejects_invalid_configuration():
    with pytest.raises(ValueError, match="k must be positive"):
        weighted_rank_fusion({}, k=0)
    with pytest.raises(ValueError, match="must not be negative"):
        weighted_rank_fusion({"semantic": {"doc": 1}}, weights={"semantic": -1})
