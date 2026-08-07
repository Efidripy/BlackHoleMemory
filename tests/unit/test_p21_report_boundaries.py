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
    _load("validate_bhm_p21_14", "validate-bhm-p21.14-source-reclassification.py"),
    _load("validate_bhm_p21_15", "validate-bhm-p21.15-parser-parity.py"),
    _load("validate_bhm_p21_16", "validate-bhm-p21.16-change-impact.py"),
    _load("validate_bhm_p21_17", "validate-bhm-p21.17-source-delta.py"),
    _load("validate_bhm_p21_18", "validate-bhm-p21.18-source-freeze.py"),
    _load("validate_bhm_p21_19", "validate-bhm-p21.19-toolchain-ledger.py"),
]


def _make_hardlink(target: Path, source: Path) -> None:
    source.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")


@pytest.mark.parametrize("module", MODULES)
def test_p21_report_writer_rejects_hardlink_target(module, tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    outside = tmp_path / "outside.json"
    _make_hardlink(target, outside)

    with pytest.raises(OSError, match="hardlink"):
        module._write_report(target, {"ok": True})
    assert outside.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.parametrize("module", MODULES)
def test_p21_report_writer_creates_nested_json(module, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "report.json"
    module._write_report(target, {"ok": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
