from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from blackholememory import app as bhm_app
from blackholememory.filesystem_boundaries import FilesystemBoundaryError


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_galaxy_code_project_snapshot_query_uses_registry_busy_timeout() -> None:
    source = (REPO_ROOT / "src" / "blackholememory" / "app.py").read_text(encoding="utf-8")
    assert "SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS" in source
    assert "PRAGMA busy_timeout={int(SQLITE_DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)}" in source


def test_galaxy_code_project_snapshot_query_rejects_hardlinked_database(tmp_path, monkeypatch) -> None:
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"do-not-read")
    target = tmp_path / "memories.sqlite3"
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    monkeypatch.setattr(bhm_app, "resolve_runtime_storage_config", lambda **_kwargs: SimpleNamespace(database_path=target))
    with pytest.raises(FilesystemBoundaryError, match="hardlink"):
        bhm_app._load_galaxy_code_project_nodes_sync(None, 5)
