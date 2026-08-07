from __future__ import annotations

import os

import pytest

from blackholememory.filesystem_boundaries import assert_safe_path
from blackholememory.filesystem_boundaries import FilesystemBoundaryError
from blackholememory.hook_queue import HookJobQueue
from blackholememory.observation_store import ObservationStore


@pytest.mark.skipif(os.name != "nt", reason="UNC path syntax is Windows-specific")
@pytest.mark.parametrize(
    "raw_path",
    [
        r"\\server\share\folder\file.sqlite3",
        r"\\?\UNC\server\share\folder\file.sqlite3",
    ],
)
def test_filesystem_boundary_rejects_unc_paths(raw_path: str) -> None:
    with pytest.raises(FilesystemBoundaryError, match="UNC"):
        assert_safe_path(raw_path)


@pytest.mark.parametrize("store_factory", [ObservationStore, HookJobQueue])
def test_sqlite_backup_rejects_hardlinked_target(tmp_path, store_factory) -> None:
    store = store_factory(tmp_path / "source.sqlite3")
    store.initialize()
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"do-not-overwrite")
    target = tmp_path / "backup.sqlite3"
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(FilesystemBoundaryError, match="hardlink"):
        store.backup_to(target)
    assert outside.read_bytes() == b"do-not-overwrite"


@pytest.mark.parametrize("store_factory", [ObservationStore, HookJobQueue])
def test_sqlite_backup_rejects_reparse_parent(tmp_path, store_factory) -> None:
    store = store_factory(tmp_path / "source.sqlite3")
    store.initialize()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(FilesystemBoundaryError, match="symlink|reparse"):
        store.backup_to(linked_parent / "backup.sqlite3")
    assert not (outside / "backup.sqlite3").exists()
