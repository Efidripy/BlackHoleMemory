from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

from blackholememory import galaxy_atlas
from blackholememory.galaxy_atlas import AtlasCache, build_atlas, store_digest

STATIC = Path(__file__).resolve().parents[2] / "src" / "blackholememory" / "static"


def _database(path: Path, rows: int = 6) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE memories (memory_id TEXT, project TEXT, memory_type TEXT, title TEXT, created_at TEXT,
            updated_at TEXT, memory_class TEXT, metadata_json TEXT, lifecycle TEXT);
        CREATE TABLE memory_links (source_id TEXT, target_id TEXT, relation TEXT);
        """
    )
    for i in range(rows):
        connection.execute(
            "INSERT INTO memories VALUES (?, ?, 'fact', ?, '2026-10-01T00:00:00Z', ?, 'semantic', '{}', 'active')",
            (f"m{i}", "alpha" if i % 2 else "beta", f"title {i}", f"2026-10-0{i + 1}T00:00:00Z"),
        )
    connection.execute("INSERT INTO memory_links VALUES ('m0', 'm1', 'depends_on')")
    connection.commit()
    connection.close()
    return path


def test_digest_changes_when_the_store_changes(tmp_path: Path) -> None:
    db = _database(tmp_path / "m.sqlite3")
    before = store_digest(db)
    connection = sqlite3.connect(db)
    connection.execute("UPDATE memories SET updated_at = '2027-01-01T00:00:00Z' WHERE memory_id = 'm0'")
    connection.commit()
    connection.close()
    assert store_digest(db) != before


def test_build_atlas_lays_out_every_active_memory_without_vectors(tmp_path: Path, monkeypatch) -> None:
    db = _database(tmp_path / "m.sqlite3")
    monkeypatch.setattr(galaxy_atlas, "LAYOUT_ITERATIONS", 5)
    monkeypatch.setattr(galaxy_atlas, "_load_vectors", lambda *_: {})
    atlas = build_atlas(db, "http://127.0.0.1:1")
    assert atlas["projects"] == ["alpha", "beta"] and atlas["psize"] == [3, 3]
    assert len(atlas["nodes"]["id"]) == 6 and len(atlas["pos"]) == 18 and len(atlas["hubs"]) == 6
    assert atlas["edges"]["kind"] == [1] and np.isfinite(atlas["pos"]).all()


def test_build_atlas_adds_similarity_edges_from_vectors(tmp_path: Path, monkeypatch) -> None:
    db = _database(tmp_path / "m.sqlite3")
    rng = np.random.default_rng(1)
    vectors = {f"m{i}": rng.normal(0, 1, 8).astype(np.float32) for i in range(6)}
    monkeypatch.setattr(galaxy_atlas, "LAYOUT_ITERATIONS", 5)
    monkeypatch.setattr(galaxy_atlas, "_load_vectors", lambda *_: vectors)
    atlas = build_atlas(db, "http://127.0.0.1:1")
    assert 7 in atlas["edges"]["kind"] and len(atlas["edges"]["a"]) >= 6


def test_cache_serves_building_then_ready_and_does_not_rebuild_a_fresh_snapshot(tmp_path: Path, monkeypatch) -> None:
    db = _database(tmp_path / "m.sqlite3")
    calls: list[int] = []

    def fake_build(*_: object) -> dict:
        calls.append(1)
        return {"schema": "x", "projects": [], "nodes": {"id": []}}

    monkeypatch.setattr(galaxy_atlas, "build_atlas", fake_build)
    cache = AtlasCache(tmp_path / "c" / "atlas.json", db, "http://127.0.0.1:1")
    assert cache.snapshot()["status"] == "building"
    cache._thread.join(5)
    ready = cache.snapshot()
    assert ready["status"] == "ready" and ready["digest"] == store_digest(db)
    cache.snapshot()
    assert len(calls) == 1
    assert json.loads((tmp_path / "c" / "atlas.json").read_text(encoding="utf-8"))["schema"] == "x"


def test_cache_failure_is_reported_not_raised(tmp_path: Path, monkeypatch) -> None:
    db = _database(tmp_path / "m.sqlite3")

    def boom(*_: object) -> dict:
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(galaxy_atlas, "build_atlas", boom)
    cache = AtlasCache(tmp_path / "atlas.json", db, "http://127.0.0.1:1")
    cache.snapshot()
    cache._thread.join(5)
    assert cache.snapshot()["status"] == "error" and "qdrant down" in cache.snapshot()["error"]


def test_atlas_page_has_no_tuning_panel_and_uses_the_session_contract() -> None:
    html = (STATIC / "galaxy-atlas.html").read_text(encoding="utf-8")
    assert 'id="tune"' not in html and "ВЕСА И ФИЗИКА" not in html
    assert '"/bhm/galaxy/atlas"' in html
    assert '"/bhm/ui/session/exchange"' in html and "bootstrap_token: token" in html
    assert 'src="/bhm/galaxy/classic"' in html
    assert "bhm-ui-bootstrap" in html and "location.hash.slice(1)" in html
