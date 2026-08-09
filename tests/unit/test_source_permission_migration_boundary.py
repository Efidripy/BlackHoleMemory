from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "migrate-bhm-source-permission-metadata.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("bhm_test_source_permission_migration", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_permission_writer_preserves_utf8_and_requested_newline(tmp_path: Path) -> None:
    module = _load_module()
    target = tmp_path / "nested" / "SOURCE-MANIFEST.json"

    module._write(target, '{"label": "галактика"}\n', "\r\n")

    assert target.read_bytes() == '{"label": "галактика"}\r\n'.encode("utf-8")


def test_source_permission_writer_rejects_hardlink_target(tmp_path: Path) -> None:
    module = _load_module()
    target = tmp_path / "SOURCE-MANIFEST.json"
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(sentinel)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        module._write(target, '{"changed": true}\n', "\n")
    assert sentinel.read_text(encoding="utf-8") == "sentinel"


def test_source_permission_writer_rejects_reparse_parent(tmp_path: Path) -> None:
    module = _load_module()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "manifests"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(OSError, match="symlink|junction|reparse"):
        module._write(linked / "SOURCE-MANIFEST.json", '{"changed": true}\n', "\n")
    assert not (outside / "SOURCE-MANIFEST.json").exists()
