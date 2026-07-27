from __future__ import annotations

from pathlib import Path

from blackholememory.code_search import get_repository_snippet
from blackholememory.code_search import search_repository_code


def _files() -> list[dict[str, object]]:
    return [{"path": "module.py", "language": "Python", "size_bytes": 56, "content_sha256": "a" * 64}]


def test_code_search_is_bounded_and_does_not_return_source_by_default(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "module.py").write_text("def keep():\n    token = 'sk-secret-value'\n    return token\n", encoding="utf-8")

    result = search_repository_code(root, _files(), query="keep", snapshot_digest="snap")

    assert result["matches"][0]["path"] == "module.py"
    assert "snippet" not in result["matches"][0]
    assert result["execution"]["source_persisted"] is False
    assert result["execution"]["raw_source_returned"] is False


def test_code_snippet_is_explicitly_redacted(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "module.py").write_text("def keep():\n    authorization = 'Bearer secret'\n    return 1\n", encoding="utf-8")

    result = get_repository_snippet(root, _files(), path="module.py", line=2, context=0, snapshot_digest="snap")

    assert result["snippet_mode"] == "redacted"
    assert "secret" not in result["snippet"]
    assert "[REDACTED]" in result["snippet"]
    assert result["execution"]["source_persisted"] is False


def test_code_search_uses_bounded_ranked_strategy_across_all_indexed_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "rare.py").write_text("def target():\n    return target\n", encoding="utf-8")
    (root / "common.py").write_text("target target target\n", encoding="utf-8")
    files = [
        {"path": "rare.py", "language": "Python", "size_bytes": 32, "content_sha256": "a" * 64},
        {"path": "common.py", "language": "Python", "size_bytes": 24, "content_sha256": "b" * 64},
    ]

    result = search_repository_code(root, files, query="target", limit=2)

    assert result["search_strategy"] == "bounded-bm25"
    assert len(result["matches"]) == 2
    assert all(float(match["score"]) > 0 for match in result["matches"])


def test_code_search_paginates_text_symbol_and_path_modes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    contents = {
        "a.py": "def target_a():\n    return 1\n",
        "b.py": "def target_b():\n    return 2\n",
        "target.txt": "target marker\n",
    }
    files = []
    for path, content in contents.items():
        (root / path).write_text(content, encoding="utf-8")
        files.append({"path": path, "language": "Python", "size_bytes": len(content.encode("utf-8")), "content_sha256": "a" * 64})

    text_page = search_repository_code(root, files, query="target", mode="text", limit=1, offset=1)
    assert text_page["offset"] == 1
    assert len(text_page["matches"]) == 1
    assert text_page["next_offset"] == 2
    assert text_page["execution"]["raw_source_returned"] is False

    symbol_page = search_repository_code(root, files, query="target", mode="symbol", limit=1, offset=1)
    assert symbol_page["offset"] == 1
    assert len(symbol_page["matches"]) == 1
    assert symbol_page["next_offset"] is None

    path_page = search_repository_code(root, files, query=".py", mode="path", limit=1, offset=1)
    assert path_page["offset"] == 1
    assert len(path_page["matches"]) == 1
    assert path_page["next_offset"] is None


def test_code_search_semantic_fusion_is_feature_flagged_and_metadata_only(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "module.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
    files = [{"path": "module.py", "language": "Python", "size_bytes": 28, "content_sha256": "a" * 64}]
    semantic_hits = [
        {
            "score": 0.99,
            "content": "must never be copied into the result",
            "metadata": {"files": ["module.py"], "source_id": "memory-1"},
        }
    ]

    disabled = search_repository_code(root, files, query="keep", semantic_hits=semantic_hits)
    assert disabled["semantic_fusion"]["active"] is False
    assert disabled["execution"]["semantic_fusion"] is False
    assert "content" not in disabled["matches"][0]

    monkeypatch.setenv("BHM_CODE_SEMANTIC_FUSION", "1")
    enabled = search_repository_code(root, files, query="keep", semantic_hits=semantic_hits)
    assert enabled["semantic_fusion"]["active"] is True
    assert enabled["search_strategy"] == "bounded-bm25+qdrant-rrf"
    assert enabled["matches"][0]["metadata"]["fusion_channels"] == ["lexical", "qdrant-semantic"]
    assert "must never be copied" not in str(enabled)
