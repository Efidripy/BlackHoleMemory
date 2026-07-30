from __future__ import annotations

from pathlib import Path

import pytest

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


def test_compile_bounded_regex_preserves_simple_filters_and_rejects_nested_repetition() -> None:
    assert compile_bounded_regex("keep|helper", field="name_pattern").search("helper")
    with pytest.raises(SecurityBoundaryError, match="unsafe nested repetition"):
        compile_bounded_regex("(a+)+$", field="name_pattern")
