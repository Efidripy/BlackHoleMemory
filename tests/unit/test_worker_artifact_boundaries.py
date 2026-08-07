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
