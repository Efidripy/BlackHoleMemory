from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "normalize-bhm-text-encoding.py"


def _module():
    spec = importlib.util.spec_from_file_location("normalize_bhm_text_encoding", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_normalizer_applies_with_atomic_backup_and_manifest(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "source"
    root.mkdir()
    source = root / "note.txt"
    original = b"\xef\xbb\xbfhello\n"
    source.write_bytes(original)

    changes = module.plan(root)
    assert [item["path"] for item in changes] == ["note.txt"]

    backup_root = tmp_path / "backup"
    module.apply(root, changes, backup_root)

    assert source.read_bytes() == b"hello\n"
    assert (backup_root / "note.txt").read_bytes() == original
    manifest = json.loads((backup_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest[0]["path"] == "note.txt"
    assert manifest[0]["after_sha256"]


def test_normalizer_rejects_hardlinked_source_before_writes(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "source"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel", encoding="utf-8")
    source = root / "note.txt"
    try:
        source.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        module.apply(root, [{"path": "note.txt", "before_sha256": ""}], tmp_path / "backup")

    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert not (tmp_path / "backup" / "note.txt").exists()


def test_normalizer_rejects_hardlinked_backup_target_before_source_write(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "source"
    root.mkdir()
    source = root / "note.txt"
    source.write_text("source", encoding="utf-8")
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel", encoding="utf-8")
    try:
        (backup_root / "note.txt").hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        module.apply(root, [{"path": "note.txt", "before_sha256": ""}], backup_root)

    assert source.read_text(encoding="utf-8") == "source"
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_normalizer_rejects_symlinked_backup_root_before_source_write(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "source"
    root.mkdir()
    source = root / "note.txt"
    source.write_text("source", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    backup_root = tmp_path / "backup-link"
    try:
        backup_root.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(OSError, match="symlink|reparse"):
        module.apply(root, [{"path": "note.txt", "before_sha256": ""}], backup_root)

    assert source.read_text(encoding="utf-8") == "source"
    assert not list(outside.iterdir())


def test_normalizer_rejects_hardlinked_manifest_before_source_write(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "source"
    root.mkdir()
    source = root / "note.txt"
    source.write_text("source", encoding="utf-8")
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    outside = tmp_path / "manifest-sentinel"
    outside.write_text("sentinel", encoding="utf-8")
    try:
        (backup_root / "manifest.json").hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        module.apply(root, [{"path": "note.txt", "before_sha256": ""}], backup_root)

    assert source.read_text(encoding="utf-8") == "source"
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_normalizer_rejects_directory_manifest_before_source_write(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "source"
    root.mkdir()
    source = root / "note.txt"
    source.write_text("source", encoding="utf-8")
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    (backup_root / "manifest.json").mkdir()

    with pytest.raises(ValueError, match="manifest target is not a regular file"):
        module.apply(root, [{"path": "note.txt", "before_sha256": ""}], backup_root)

    assert source.read_text(encoding="utf-8") == "source"


def test_normalizer_rejects_stale_plan_before_backup_write(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "source"
    root.mkdir()
    source = root / "note.txt"
    source.write_text("before", encoding="utf-8")
    changes = module.plan(root)
    assert changes == []
    change = {"path": "note.txt", "before_sha256": "0" * 64}
    source.write_text("after", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed since plan"):
        module.apply(root, [change], tmp_path / "backup")

    assert source.read_text(encoding="utf-8") == "after"
    assert not (tmp_path / "backup" / "note.txt").exists()


def test_normalizer_rejects_traversal_change_path(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "source"
    root.mkdir()
    source = root / "note.txt"
    source.write_text("source", encoding="utf-8")

    with pytest.raises(ValueError, match="escapes"):
        module.apply(root, [{"path": "../note.txt", "before_sha256": ""}], tmp_path / "backup")

    assert source.read_text(encoding="utf-8") == "source"
