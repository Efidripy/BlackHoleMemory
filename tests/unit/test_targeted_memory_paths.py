from __future__ import annotations

from pathlib import Path

from blackholememory import app as bhm_app


class _TargetedService:
    def __init__(self) -> None:
        self.records = {
            "memory-1": {
                "source_id": "memory-1",
                "project": "blackholememory",
                "content": "content",
                "metadata": {"upsert_key": "key-1"},
            }
        }
        self.saved: list[list[dict]] = []

    def get_record(self, memory_id: str, *, project: str | None = None):
        record = self.records.get(memory_id)
        if record is None or (project and record["project"] != project):
            return None
        return record

    def get_record_by_upsert_key(self, project: str, upsert_key: str):
        return next(
            (
                record
                for record in self.records.values()
                if record["project"] == project
                and record["metadata"].get("upsert_key") == upsert_key
            ),
            None,
        )

    def upsert_records(self, records):
        items = list(records)
        self.saved.append(items)
        for record in items:
            self.records[record["source_id"]] = record
        return Path("memories.sqlite3")


def test_targeted_find_and_replace_avoid_full_store_materialization(monkeypatch) -> None:
    service = _TargetedService()
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: service)
    monkeypatch.setattr(
        bhm_app,
        "_load_live_memories",
        lambda: (_ for _ in ()).throw(AssertionError("full store load used")),
    )
    monkeypatch.setattr(bhm_app, "_emit_memory_pulse", lambda *_args: None)

    record = bhm_app._find_live_memory("memory-1", "BlackHoleMemory")
    assert record is service.records["memory-1"]
    assert bhm_app._find_live_memory_by_upsert_key("BlackHoleMemory", "key-1") is record

    updated = {**record, "content": "updated"}
    bhm_app._replace_live_memory(updated)

    assert service.saved == [[updated]]


def test_save_live_memories_delegates_atomic_diffing_to_service(monkeypatch) -> None:
    service = _TargetedService()
    monkeypatch.setattr(bhm_app, "_memory_service", lambda: service)
    items = [service.records["memory-1"]]

    result = bhm_app._save_live_memories(items)

    assert result == Path("memories.sqlite3")
    assert service.saved == [items]
