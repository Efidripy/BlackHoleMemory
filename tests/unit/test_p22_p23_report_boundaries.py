from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULES = [
    _load("validate_bhm_graph_activation", "validate-bhm-graph-activation.py"),
    _load("validate_bhm_graph_continuity", "validate-bhm-graph-continuity.py"),
    _load("validate_bhm_small_repository", "validate-bhm-small-repository.py"),
    _load("validate_bhm_compatibility_disposition", "validate-bhm-compatibility-disposition.py"),
]


def _make_hardlink(target: Path, source: Path) -> None:
    source.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")


@pytest.mark.parametrize("module", MODULES)
def test_p22_p23_report_writer_rejects_hardlink_target(module, tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    outside = tmp_path / "outside.json"
    _make_hardlink(target, outside)

    with pytest.raises(OSError, match="hardlink"):
        module._write_report(target, {"ok": True})
    assert outside.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.parametrize("module", MODULES)
def test_p22_p23_report_writer_creates_nested_json(module, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "report.json"
    module._write_report(target, {"ok": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
