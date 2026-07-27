from __future__ import annotations

import pytest

from blackholememory.retrieval_diversity import mmr_select
from blackholememory.retrieval_diversity import token_jaccard_similarity


def test_token_jaccard_similarity_is_bounded_and_deterministic():
    assert token_jaccard_similarity("Qdrant Mem0", "qdrant mem0 contract") == pytest.approx(2 / 3)
    assert token_jaccard_similarity("", "content") == 0.0
    assert 0.0 <= token_jaccard_similarity("a b", "b c") <= 1.0


def test_mmr_promotes_a_diverse_candidate_when_relevance_is_close():
    selections = mmr_select(
        ["qdrant mem0 contract", "qdrant mem0 contract details", "playwright browser flow"],
        [1.0, 0.98, 0.65],
        lambda_param=0.6,
    )

    assert [selection.index for selection in selections] == [0, 2, 1]
    assert selections[1].redundancy == 0.0
    assert all(selection.mmr_score <= 1.0 for selection in selections)


def test_mmr_rejects_mismatched_inputs_and_invalid_lambda():
    with pytest.raises(ValueError, match="equal length"):
        mmr_select(["one"], [])
    with pytest.raises(ValueError, match="between 0 and 1"):
        mmr_select(["one"], [1.0], lambda_param=1.1)
