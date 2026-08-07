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


LIFECYCLE = _load("validate_bhm_lifecycle_matrix", "validate-bhm-lifecycle-matrix.py")
CI_RECEIPT = _load("validate_ci_receipt_binding", "validate-ci-receipt-binding.py")


def _make_hardlink(target: Path, source: Path) -> None:
    source.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")


def test_lifecycle_report_writer_rejects_hardlink_target(tmp_path: Path) -> None:
    target = tmp_path / "lifecycle.json"
    outside = tmp_path / "outside.json"
    _make_hardlink(target, outside)

    with pytest.raises(OSError, match="hardlink"):
        LIFECYCLE._write_report(target, '{"ok":true}')
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_lifecycle_report_writer_creates_nested_report(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "lifecycle.json"
    LIFECYCLE._write_report(target, '{"ok":true}')

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_ci_receipt_writer_rejects_hardlink_target(tmp_path: Path) -> None:
    target = tmp_path / "receipt.json"
    outside = tmp_path / "outside.json"
    _make_hardlink(target, outside)

    with pytest.raises(OSError, match="hardlink"):
        CI_RECEIPT._write_receipt(target, {"schema_version": "test"})
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_ci_receipt_writer_creates_nested_receipt(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "receipt.json"
    CI_RECEIPT._write_receipt(target, {"schema_version": "test"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"schema_version": "test"}
