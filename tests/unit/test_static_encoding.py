from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts/validate-bhm-static-encoding.py"
SPEC = importlib.util.spec_from_file_location("bhm_static_encoding", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise AssertionError(f"unable to load {SCRIPT_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_static_assets_are_utf8_and_free_of_mojibake():
    result = MODULE.validate(REPO_ROOT / "src/blackholememory/static")

    assert result["ok"] is True
    assert result["files"] >= 3
    assert result["failures"] == []


def test_encoding_gate_reports_invalid_utf8_and_mojibake(tmp_path):
    (tmp_path / "galaxy.html").write_bytes(b"<html lang=\"en\"><meta charset=\"UTF-8\">\xff")
    (tmp_path / "panel.css").write_text(".note { content: 'Ðž'; }", encoding="utf-8")

    result = MODULE.validate(tmp_path)

    assert result["ok"] is False
    assert any("invalid UTF-8" in failure for failure in result["failures"])
    assert any("mojibake marker" in failure for failure in result["failures"])
