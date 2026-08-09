from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "restore_bhm_public_evidence.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("bhm_test_restore_public_evidence", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sanitize_existing_ops_preserves_utf8_and_rewrites_only_sensitive_text(tmp_path: Path) -> None:
    module = _load_module()
    ops = tmp_path / "ops"
    ops.mkdir()
    target = ops / "receipt.md"
    target.write_text("owner=C:\\Users\\alice\\BHM\n", encoding="utf-8")

    assert module.sanitize_existing_ops(ops) == 1
    assert target.read_text(encoding="utf-8") == "owner=<user-profile>\\BHM\n"


def test_sanitize_existing_ops_rejects_hardlink_target(tmp_path: Path) -> None:
    module = _load_module()
    ops = tmp_path / "ops"
    ops.mkdir()
    target = ops / "receipt.md"
    sentinel = tmp_path / "sentinel.md"
    sentinel.write_text("owner=C:\\Users\\alice\\BHM\n", encoding="utf-8")
    try:
        target.hardlink_to(sentinel)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        module.sanitize_existing_ops(ops)
    assert sentinel.read_text(encoding="utf-8") == "owner=C:\\Users\\alice\\BHM\n"


def test_sanitize_existing_ops_rejects_reparse_ops_root(tmp_path: Path) -> None:
    module = _load_module()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "receipt.md"
    target.write_text("owner=C:\\Users\\alice\\BHM\n", encoding="utf-8")
    linked_ops = tmp_path / "ops"
    try:
        linked_ops.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(OSError, match="symlink|junction|reparse"):
        module.sanitize_existing_ops(linked_ops)
    assert target.read_text(encoding="utf-8") == "owner=C:\\Users\\alice\\BHM\n"
