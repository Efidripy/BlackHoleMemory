from __future__ import annotations

import pytest

from blackholememory import retrieval_diversity
from blackholememory.retrieval_diversity import mmr_select, token_jaccard_similarity


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


def test_mmr_matches_naive_token_jaccard_reference_for_limits_and_lambda():
    texts = [
        "repeat repeat alpha",
        "repeat beta",
        "",
        "gamma " * 2000,
        "delta repeat",
    ]
    relevance = [1.0, 0.92, 0.7, 0.68, 0.6]
    lambda_param = 0.43
    limit = 3

    maximum = max(relevance)
    normalized = [max(score, 0.0) / maximum for score in relevance]
    remaining = set(range(len(texts)))
    selected: list[int] = []
    expected = []
    while remaining and len(selected) < limit:
        def score(index: int) -> float:
            redundancy = max(
                (token_jaccard_similarity(texts[index], texts[other]) for other in selected),
                default=0.0,
            )
            return lambda_param * normalized[index] - (1.0 - lambda_param) * redundancy

        best_index = max(remaining, key=lambda index: (score(index), normalized[index], -index))
        redundancy = max(
            (token_jaccard_similarity(texts[best_index], texts[other]) for other in selected),
            default=0.0,
        )
        expected.append((best_index, round(score(best_index), 6), round(redundancy, 6)))
        selected.append(best_index)
        remaining.remove(best_index)

    actual = [(item.index, item.mmr_score, item.redundancy) for item in mmr_select(
        texts, relevance, lambda_param=lambda_param, limit=limit
    )]
    assert actual == expected


def test_mmr_tokenizes_each_text_once(monkeypatch):
    original = retrieval_diversity._TOKEN_RE
    calls: list[str] = []

    class CountingPattern:
        def findall(self, text: str):
            calls.append(text)
            return original.findall(text)

    monkeypatch.setattr(retrieval_diversity, "_TOKEN_RE", CountingPattern())
    texts = ["one two", "two three", "", "four " * 1500]
    mmr_select(texts, [1.0, 0.9, 0.8, 0.7], limit=2)

    assert len(calls) == len(texts)
