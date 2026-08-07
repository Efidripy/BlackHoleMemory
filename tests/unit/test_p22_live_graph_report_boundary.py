from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "build_bhm_p22_live_graphs",
    ROOT / "scripts" / "build-bhm-p22-live-graphs.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _make_hardlink(target: Path, source: Path) -> None:
    source.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")


def test_p22_live_graph_report_writer_rejects_hardlink_target(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    outside = tmp_path / "outside.json"
    _make_hardlink(target, outside)

    with pytest.raises(OSError, match="hardlink"):
        MODULE._write_report(target, {"ok": True})
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_p22_live_graph_report_writer_creates_nested_json(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "report.json"
    MODULE._write_report(target, {"ok": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
