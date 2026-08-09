from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.agents import developer_agent


def _hardlink(target: Path, source: Path) -> None:
    source.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")


def _linked_directory(target: Path, source: Path) -> None:
    source.mkdir()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable on this Windows host")


def test_chronicle_logger_rejects_hardlink_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(developer_agent, "_repo_root", lambda: tmp_path)
    chronicle = tmp_path / ".runtime" / "logs" / "agents" / "task" / "chronicle.md"
    chronicle.parent.mkdir(parents=True)
    _hardlink(chronicle, tmp_path / "outside.md")

    with pytest.raises(OSError, match="hardlink"):
        developer_agent.ChronicleLogger("task")


def test_quarantine_gateway_artifact_writer_rejects_hardlink_target(tmp_path: Path) -> None:
    target = tmp_path / "quarantine.json"
    _hardlink(target, tmp_path / "outside.json")
    node = developer_agent.QuarantineGatewayNode(quarantine_file=target)

    with pytest.raises(OSError, match="hardlink"):
        node._atomic_write_json([])


def test_chronicle_logger_rejects_linked_parent_before_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(developer_agent, "_repo_root", lambda: tmp_path)
    outside = tmp_path / "outside"
    linked_root = tmp_path / ".runtime" / "logs" / "agents"
    _linked_directory(linked_root, outside)

    with pytest.raises(OSError, match="symlink|junction|reparse"):
        developer_agent.ChronicleLogger("task")
    assert not (outside / "task" / "chronicle.md").exists()


def test_quarantine_gateway_rejects_linked_parent_before_creation(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    linked_root = tmp_path / "linked-parent"
    _linked_directory(linked_root, outside)

    with pytest.raises(OSError, match="symlink|junction|reparse"):
        developer_agent.QuarantineGatewayNode(quarantine_file=linked_root / "quarantine.json")
    assert not (outside / "quarantine.json").exists()
