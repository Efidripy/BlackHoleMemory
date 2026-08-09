from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory import app as bhm_app
from blackholememory.filesystem_boundaries import append_bytes_safely
from blackholememory.filesystem_boundaries import FilesystemBoundaryError
from blackholememory.filesystem_boundaries import FilesystemReadLimitError
from blackholememory.filesystem_boundaries import read_bytes_safely
from blackholememory.security_boundaries import SecurityBoundaryError
from blackholememory.security_boundaries import compile_bounded_regex
from blackholememory.security_boundaries import resolve_under_root


def test_resolve_under_root_accepts_relative_and_absolute_paths(tmp_path: Path) -> None:
    root = tmp_path / "admin-exports"
    root.mkdir()
    relative = resolve_under_root(root, "snapshot.json")
    absolute = resolve_under_root(root, str(relative))

    assert relative == absolute
    assert relative.parent == root.resolve()


@pytest.mark.parametrize("value", ["..\\outside.json", "../outside.json", "nested/../../outside.json"])
def test_resolve_under_root_rejects_escape(value: str, tmp_path: Path) -> None:
    with pytest.raises(SecurityBoundaryError):
        resolve_under_root(tmp_path / "admin-exports", value)


def test_resolve_under_root_treats_backslash_as_a_portable_separator(tmp_path: Path) -> None:
    root = tmp_path / "admin-exports"
    root.mkdir()

    resolved = resolve_under_root(root, r"nested\snapshot.json")

    assert resolved == (root / "nested" / "snapshot.json").resolve()


def test_resolve_under_root_rejects_foreign_windows_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / "admin-exports"
    root.mkdir()
    drive = Path(root).drive.upper() or "Z:"
    foreign_drive = "Y:" if drive == "Z:" else "Z:"

    with pytest.raises(SecurityBoundaryError):
        resolve_under_root(root, rf"{foreign_drive}\outside.json")


def test_resolve_under_root_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "admin-exports"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(SecurityBoundaryError):
        resolve_under_root(root, "linked/secret.json")


def test_app_json_writer_rejects_hardlink_target(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        bhm_app._write_json_atomic(target, {"state": "new"})
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_append_writer_rejects_hardlink_target(tmp_path: Path) -> None:
    target = tmp_path / "log.txt"
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        append_bytes_safely(target, b"blocked")
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_append_writer_creates_and_appends_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "log.txt"
    append_bytes_safely(target, b"one\n")
    append_bytes_safely(target, b"two\n")
    assert target.read_bytes() == b"one\ntwo\n"


def test_bounded_reader_preserves_regular_files_and_enforces_limit(tmp_path: Path) -> None:
    target = tmp_path / "snapshot.json"
    target.write_bytes(b'{"ok":true}')

    assert read_bytes_safely(target, max_bytes=32) == b'{"ok":true}'
    with pytest.raises(FilesystemReadLimitError, match="byte limit"):
        read_bytes_safely(target, max_bytes=4)


def test_bounded_reader_rejects_symlink_and_hardlink_targets(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"secret")
    linked = tmp_path / "linked.json"
    hardlinked = tmp_path / "hardlinked.json"
    try:
        linked.symlink_to(outside)
        hardlinked.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"linked files unavailable: {exc}")

    with pytest.raises(FilesystemBoundaryError, match="symlink|reparse"):
        read_bytes_safely(linked)
    with pytest.raises(FilesystemBoundaryError, match="hardlink"):
        read_bytes_safely(hardlinked)


def test_compile_bounded_regex_preserves_simple_filters_and_rejects_nested_repetition() -> None:
    assert compile_bounded_regex("keep|helper", field="name_pattern").search("helper")
    with pytest.raises(SecurityBoundaryError, match="unsafe nested repetition"):
        compile_bounded_regex("(a+)+$", field="name_pattern")
