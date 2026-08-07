from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.local_model_replay import write_local_model_replay_report
from blackholememory.value_benchmark import write_value_benchmark_report


def _hardlink(target: Path, source: Path) -> None:
    source.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")


@pytest.mark.parametrize("writer", [write_local_model_replay_report, write_value_benchmark_report])
def test_benchmark_report_writer_rejects_hardlink_target(tmp_path: Path, writer) -> None:
    output_json = tmp_path / "report.json"
    output_markdown = tmp_path / "report.md"
    _hardlink(output_json, tmp_path / "outside.json")

    with pytest.raises(OSError, match="hardlink"):
        writer({"schema_version": "fixture"}, output_json, output_markdown)
    assert (tmp_path / "outside.json").read_text(encoding="utf-8") == "sentinel"
