from __future__ import annotations

import os
from pathlib import Path

import pytest

from blackholememory.filesystem_boundaries import assert_safe_path
from blackholememory.filesystem_boundaries import FilesystemBoundaryError
from blackholememory.hook_queue import HookJobQueue
from blackholememory.llm_cache import LLMCacheStore
from blackholememory.llm_job_queue import LLMJobQueue
from blackholememory.llm_learning import LLMLearningStore
from blackholememory.llm_long_tasks import LongTaskStore
from blackholememory.observation_store import ObservationStore


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_backup_destinations_use_explicit_sqlite_busy_timeouts() -> None:
    observation_source = (REPO_ROOT / "src" / "blackholememory" / "observation_store.py").read_text(encoding="utf-8")
    retirement_source = (REPO_ROOT / "src" / "blackholememory" / "project_retirement.py").read_text(encoding="utf-8")

    assert "timeout=OBSERVATION_STORE_BUSY_TIMEOUT_MS / 1000" in observation_source
    assert "PRAGMA busy_timeout={OBSERVATION_STORE_BUSY_TIMEOUT_MS}" in observation_source
    assert "timeout=SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS" in retirement_source
    assert "PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}" in retirement_source


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


@pytest.mark.parametrize(
    "store_factory",
    [ObservationStore, HookJobQueue, LLMJobQueue, LLMCacheStore, LLMLearningStore, LongTaskStore],
)
def test_sqlite_store_rejects_reparse_parent_before_initialization(tmp_path, store_factory) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    store = store_factory(linked_parent / "store.sqlite3")
    with pytest.raises(FilesystemBoundaryError, match="symlink|reparse"):
        store.initialize()
    assert not (outside / "store.sqlite3").exists()


@pytest.mark.parametrize(
    "store_factory",
    [ObservationStore, HookJobQueue, LLMJobQueue, LLMCacheStore, LLMLearningStore, LongTaskStore],
)
def test_sqlite_store_rejects_hardlinked_target_before_initialization(tmp_path, store_factory) -> None:
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"do-not-touch")
    target = tmp_path / "store.sqlite3"
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    store = store_factory(target)
    with pytest.raises(FilesystemBoundaryError, match="hardlink"):
        store.initialize()
    assert outside.read_bytes() == b"do-not-touch"
