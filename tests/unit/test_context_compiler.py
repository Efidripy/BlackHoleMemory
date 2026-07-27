from __future__ import annotations

from blackholememory.context_compiler import compile_context
from blackholememory.context_compiler import estimate_tokens


def test_estimate_tokens_is_deterministic_and_conservative_for_budgeting():
    assert estimate_tokens("") == 0
    assert estimate_tokens("1234") == 1
    assert estimate_tokens("12345") == 2


def test_compile_context_keeps_text_within_budget_and_returns_citations():
    result = compile_context(
        [
            {
                "id": "memory-a",
                "title": "Decision A",
                "project": "blackholememory",
                "content": "Use the canonical BHM retrieval path.",
                "score": 0.91,
                "context_origin": "LOCAL",
                "metadata": {
                    "source_refs": ["references/architecture/0040.md"],
                    "files": ["src/blackholememory/app.py"],
                    "source_system": "bhm",
                    "provenance": "mcp",
                    "session_refs": ["session-1"],
                    "secret_like": "must not be copied",
                },
            },
            {
                "id": "memory-b",
                "title": "Background",
                "project": "blackholememory",
                "content": "A bounded context is easier to validate and cache.",
                "score": 0.72,
                "context_origin": "GLOBAL",
            },
        ],
        token_budget=40,
    )

    assert result["text"]
    assert result["estimated_tokens"] <= result["token_budget"]
    assert result["included_count"] == len(result["citations"])
    assert result["citations"][0]["id"] == "memory-a"
    assert result["citations"][0]["source_refs"] == ["references/architecture/0040.md"]
    assert result["citations"][0]["provenance"]["source_system"] == "bhm"
    assert result["citations"][0]["provenance"]["source_kind"] == "mcp"
    assert result["citations"][0]["provenance"]["session_refs"] == ["session-1"]
    assert result["provenance"]["contract"] == "bhm.context.provenance.v1"
    assert "secret_like" not in result["citations"][0]


def test_compile_context_truncates_deterministically_and_bounds_item_size():
    result = compile_context(
        [
            {"id": "long", "content": "x" * 5000},
            {"id": "second", "content": "should not outrank the first"},
        ],
        token_budget=32,
        max_item_chars=100,
    )

    assert result["truncated"] is True
    assert result["included_count"] == 1
    assert result["estimated_tokens"] <= 32
    assert len(result["text"]) <= 32 * 4
    assert result["citations"][0]["id"] == "long"


def test_compile_context_reports_bounded_provenance_gaps_and_omissions():
    result = compile_context(
        [
            {"id": "empty", "project": "blackholememory", "content": ""},
            {
                "id": "first",
                "project": "blackholememory",
                "content": "x" * 500,
                "context_origin": "LOCAL",
                "metadata": {"source_system": "bhm", "source_refs": ["docs/plan.md"]},
            },
            {
                "id": "second",
                "project": "blackholememory",
                "content": "must be omitted by the token budget",
                "context_origin": "LOCAL",
                "metadata": {"source_system": "bhm"},
            },
        ],
        token_budget=16,
    )

    assert result["provenance"]["complete"] is True
    assert result["provenance"]["evidence_coverage"] == {
        "with_evidence": 1,
        "without_evidence": 0,
        "ratio": 1.0,
    }
    assert result["omissions"]["count"] == 2
    assert result["omissions"]["reasons"] == ["empty_content", "token_budget"]
    assert {item["id"] for item in result["omissions"]["items"]} == {"empty", "second"}
