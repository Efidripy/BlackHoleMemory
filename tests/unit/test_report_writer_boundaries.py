from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from blackholememory import mem0_adapter
from blackholememory.mem0_adapter import BHMGraphManager
from blackholememory.parser_activation import write_report


def _make_hardlink(target: Path, source: Path) -> None:
    source.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")


def test_parser_report_writer_rejects_hardlink_target(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    outside = tmp_path / "outside.json"
    _make_hardlink(target, outside)

    with pytest.raises(OSError, match="hardlink"):
        write_report({"ok": True}, target)
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_semantic_graph_writer_rejects_hardlink_target(tmp_path: Path) -> None:
    target = tmp_path / "semantic_graph.json"
    outside = tmp_path / "outside.json"
    _make_hardlink(target, outside)
    manager = BHMGraphManager(target)

    with pytest.raises(OSError, match="hardlink"):
        asyncio.run(manager.add_semantic_link("source", "target", "DEPENDS_ON"))
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_decay_archive_writer_rejects_hardlink_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "decayed-memory.jsonl"
    outside = tmp_path / "outside.jsonl"
    _make_hardlink(target, outside)
    monkeypatch.setattr(mem0_adapter, "DECAY_ARCHIVE_PATH", target)

    with pytest.raises(OSError, match="hardlink"):
        mem0_adapter._append_decayed_payload_archive(
            collection_name="bhm_local_memory_test",
            point_id="point-1",
            payload={"source_id": "memory-1"},
            score=0.1,
            threshold=0.2,
            archived_at="2026-08-06T00:00:00Z",
        )
    assert outside.read_text(encoding="utf-8") == "sentinel"
