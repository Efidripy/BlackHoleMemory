from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "sync-bhm-source-quarantine.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("bhm_test_source_quarantine_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_source_quarantine_report_writer_uses_atomic_json(tmp_path: Path) -> None:
    module = _load_module()
    target = tmp_path / "nested" / "report.json"
    module._write_report(target, {"ok": True, "events": []})

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True, "events": []}


def test_source_quarantine_report_writer_rejects_hardlink_target(tmp_path: Path) -> None:
    module = _load_module()
    target = tmp_path / "report.json"
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(sentinel)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        module._write_report(target, {"ok": False})
    assert sentinel.read_text(encoding="utf-8") == "sentinel"


def test_source_quarantine_report_writer_rejects_reparse_parent(tmp_path: Path) -> None:
    module = _load_module()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "reports"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(OSError, match="symlink|junction|reparse"):
        module._write_report(linked / "report.json", {"ok": False})
    assert not (outside / "report.json").exists()
