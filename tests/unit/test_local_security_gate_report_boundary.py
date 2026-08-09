from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-bhm-local-security-gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("bhm_test_local_security_gate_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_security_gate_report_writer_preserves_utf8_and_newline(tmp_path: Path) -> None:
    module = _load_module()
    target = tmp_path / "nested" / "gate.json"

    module._write_report(target, '{"ok": true, "label": "галактика"}\n')

    assert target.read_text(encoding="utf-8") == '{"ok": true, "label": "галактика"}\n'


def test_local_security_gate_report_writer_rejects_hardlink_target(tmp_path: Path) -> None:
    module = _load_module()
    target = tmp_path / "gate.json"
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(sentinel)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        module._write_report(target, '{"ok": false}\n')
    assert sentinel.read_text(encoding="utf-8") == "sentinel"


def test_local_security_gate_report_writer_rejects_reparse_parent(tmp_path: Path) -> None:
    module = _load_module()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "reports"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(OSError, match="symlink|junction|reparse"):
        module._write_report(linked / "gate.json", '{"ok": false}\n')
    assert not (outside / "gate.json").exists()
