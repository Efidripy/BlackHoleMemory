import time
from unittest.mock import MagicMock

from blackholememory import (
    app_core_hit,
    app_core_hook,
    app_ops_admin,
    app_runtime as rt,
    app_telemetry,
    galaxy_atlas,
)


def test_count_fallback_memory_records_with_mocked_fallback(monkeypatch):
    monkeypatch.setattr(
        app_core_hit,
        "_fallback_memory_records",
        lambda **kwargs: [{"id": "m1"}, {"id": "m2"}],
    )
    count = app_core_hit._count_fallback_memory_records(project="test-proj")
    assert count == 2


def test_count_fallback_memory_records_service_call(monkeypatch):
    monkeypatch.setattr(app_core_hit, "_fallback_memory_records", app_core_hit._ORIGINAL_FALLBACK_MEMORY_RECORDS)
    mock_service = MagicMock()
    mock_service.count_records.return_value = 42
    monkeypatch.setattr(rt, "_memory_service", lambda: mock_service)
    count = app_core_hit._count_fallback_memory_records(project="test-proj")
    assert count == 42
    mock_service.count_records.assert_called_once_with(
        project="test-proj",
        exclude_event_role="trace",
        include_archived=False,
    )


def test_json_store_cached_load(tmp_path):
    json_file = tmp_path / "test.json"
    json_file.write_text('[{"a": 1}]', encoding="utf-8")

    data1 = app_core_hook._load_json_store(json_file)
    assert data1 == [{"a": 1}]

    # Same mtime and size returns cached
    data2 = app_core_hook._load_json_store(json_file)
    assert data2 is data1

    # Modifying the file invalidates cache
    time.sleep(0.01)
    json_file.write_text('[{"a": 2}, {"b": 3}]', encoding="utf-8")
    data3 = app_core_hook._load_json_store(json_file)
    assert data3 == [{"a": 2}, {"b": 3}]


def test_agent_activity_rollup_ttl_cache(monkeypatch):
    calls = 0

    def fake_checkpoints():
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(app_core_hook, "_load_checkpoints", fake_checkpoints)
    monkeypatch.setattr(app_core_hook, "_load_session_records", lambda: [])
    mock_store = MagicMock()
    mock_store.activity_rollup.return_value = {"count": 10, "latest": "2026-10-24T12:00:00Z"}
    monkeypatch.setattr(app_core_hook, "_observation_store", lambda: mock_store)

    app_ops_admin._AGENT_ACTIVITY_ROLLUP_CACHE.clear()

    res1 = app_ops_admin._agent_activity_rollup("proj-a")
    assert res1["counts"]["observations"] == 10
    assert calls == 1

    # Second call uses TTL cache
    res2 = app_ops_admin._agent_activity_rollup("proj-a")
    assert res2 == res1
    assert calls == 1


def test_launcher_stats_ttl_cache(monkeypatch):
    calls = 0

    def fake_counts():
        nonlocal calls
        calls += 1
        return {"memory_count": 100, "session_count": 5}

    monkeypatch.setattr(app_telemetry, "_collect_global_launcher_counts_sync", fake_counts)
    app_telemetry._LAUNCHER_STATS_CACHE = None

    stats1 = app_telemetry.bhm_launcher_stats()
    assert stats1["memory_count"] == 100
    assert calls == 1

    stats2 = app_telemetry.bhm_launcher_stats()
    assert stats2 == stats1
    assert calls == 1


def test_atlas_cache_in_memory_and_cooldown(tmp_path, monkeypatch):
    cache_path = tmp_path / "atlas-cache.json"
    cache_path.write_text('{"nodes": [], "links": [], "digest": "abc"}', encoding="utf-8")

    digest_calls = 0

    def fake_store_digest(path):
        nonlocal digest_calls
        digest_calls += 1
        return "abc"

    monkeypatch.setattr(galaxy_atlas, "store_digest", fake_store_digest)

    ac = galaxy_atlas.AtlasCache(cache_path, tmp_path / "fake.sqlite3", "http://localhost:6333")
    ac._last_attempt = time.monotonic()  # inside cooldown

    res = ac.snapshot()
    assert res["status"] == "ready"
    assert digest_calls == 0  # skipped during cooldown!
