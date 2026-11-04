from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import pytest
from pydantic import ValidationError

from blackholememory import app as bhm_app
from blackholememory import app_ops, app_runtime
from blackholememory.api_models import MemoryUpdateRequest
from blackholememory.app_core_memory_write import _update_live_memory
from blackholememory.config import settings
from blackholememory.memory_service import SQLiteMemoryService
from blackholememory.temporal_migration import SQL_MANIFEST
from fastapi.testclient import TestClient

PROJECT = "test-project"
TEST_CALLER_TOKEN = "bhm-test-caller-token-0000000000000001"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_CALLER_TOKEN}"}


@pytest.fixture
def memory_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SQLiteMemoryService:
    monkeypatch.setenv("BHM_CALLER_TOKEN", TEST_CALLER_TOKEN)
    monkeypatch.setenv("BHM_TEMPORAL_CONTRACT_ENABLED", "true")
    monkeypatch.setenv("BHM_WRITE_DOC2QUERY", "0")
    monkeypatch.setattr(settings, "runtime_dir", tmp_path)

    db_path = tmp_path / "memories.sqlite3"
    service = SQLiteMemoryService(db_path, allow_create=True)
    service.repository.initialize()
    with sqlite3.connect(db_path) as conn:
        for stmt in SQL_MANIFEST:
            conn.execute(stmt)
    service.repository._memory_columns_cache = None

    # Seed card-1: card with explicit observed_at
    # Seed card-2: legacy card with observed_at=None and observed_at_source='legacy-unknown'
    service.upsert_records(
        [
            {
                "source_id": "card-1",
                "project": PROJECT,
                "memory_type": "fact",
                "content": "Initial content for card 1",
                "title": "Initial Title 1",
                "observed_at": "2026-01-15T10:00:00Z",
                "observed_at_source": "explicit",
                "created_at": "2026-01-15T10:00:00Z",
                "updated_at": "2026-01-15T10:00:00Z",
                "metadata": {
                    "raw_title": "Initial Title 1",
                },
            },
            {
                "source_id": "card-2",
                "project": PROJECT,
                "memory_type": "fact",
                "content": "Initial content for card 2",
                "title": "Initial Title 2",
                "observed_at": None,
                "observed_at_source": "legacy-unknown",
                "created_at": "2026-02-01T00:00:00Z",
                "updated_at": "2026-02-01T00:00:00Z",
                "metadata": {
                    "raw_title": "Initial Title 2",
                },
            },
        ]
    )

    monkeypatch.setattr(app_runtime, "_memory_service", lambda: service)
    monkeypatch.setattr(app_runtime, "_memory_store_is_authoritative", lambda: True)
    monkeypatch.setattr(app_ops, "_active_ontology_schema", lambda _project: None)
    return service


def _read_memory_row(db_path: Path, memory_id: str) -> sqlite3.Row:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
        assert row is not None, f"memory {memory_id} not found in SQLite"
        return row


def test_title_only_update_preserves_dates_and_revisions(memory_service: SQLiteMemoryService) -> None:
    db_path = memory_service.repository.path
    before = _read_memory_row(db_path, "card-1")

    req = MemoryUpdateRequest(id="card-1", project=PROJECT, title="Brand New Title")
    res = _update_live_memory(req)

    assert res["title"] == "Brand New Title"

    after = _read_memory_row(db_path, "card-1")
    metadata = json.loads(after["metadata_json"])

    # Title is updated in column and raw_title metadata
    assert after["title"] == "Brand New Title"
    assert metadata["raw_title"] == "Brand New Title"

    # Temporal dates and revisions are strictly preserved
    assert after["updated_at"] == before["updated_at"]
    assert after["observed_at"] == before["observed_at"]
    assert after["observed_at_source"] == before["observed_at_source"]
    assert after["current_revision_id"] == before["current_revision_id"]

    # Changelog records title_changed: True
    changelog = metadata.get("changelog") or []
    assert len(changelog) > 0
    latest = changelog[-1]
    assert latest["action"] == "update"
    assert latest["details"]["title_changed"] is True
    assert latest["details"]["content_changed"] is False


def test_title_only_update_preserves_legacy_unknown_temporal(memory_service: SQLiteMemoryService) -> None:
    db_path = memory_service.repository.path
    before = _read_memory_row(db_path, "card-2")
    assert before["observed_at"] is None
    assert before["observed_at_source"] == "legacy-unknown"

    req = MemoryUpdateRequest(id="card-2", project=PROJECT, title="New Legacy Title")
    res = _update_live_memory(req)

    assert res["title"] == "New Legacy Title"

    after = _read_memory_row(db_path, "card-2")
    metadata = json.loads(after["metadata_json"])

    assert after["title"] == "New Legacy Title"
    assert metadata["raw_title"] == "New Legacy Title"
    assert after["updated_at"] == before["updated_at"]
    assert after["observed_at"] is None
    assert after["observed_at_source"] == "legacy-unknown"
    assert after["current_revision_id"] == before["current_revision_id"]

    changelog = metadata.get("changelog") or []
    latest = changelog[-1]
    assert latest["details"]["title_changed"] is True


def test_regular_update_without_title_advances_timestamps(memory_service: SQLiteMemoryService) -> None:
    db_path = memory_service.repository.path
    before = _read_memory_row(db_path, "card-1")

    # Regular update changing content advances updated_at and observed_at
    req = MemoryUpdateRequest(id="card-1", project=PROJECT, content="Completely revised content")
    _update_live_memory(req)

    after = _read_memory_row(db_path, "card-1")
    metadata = json.loads(after["metadata_json"])

    assert after["updated_at"] != before["updated_at"]
    assert after["observed_at"] != before["observed_at"]
    assert after["current_revision_id"] != before["current_revision_id"]

    changelog = metadata.get("changelog") or []
    latest = changelog[-1]
    assert latest["details"]["title_changed"] is False
    assert latest["details"]["content_changed"] is True


def test_title_and_content_update_title_wins(memory_service: SQLiteMemoryService) -> None:
    db_path = memory_service.repository.path

    # Both title and content supplied: title must win over content-derived title
    req = MemoryUpdateRequest(
        id="card-1",
        project=PROJECT,
        title="Explicit Winning Title",
        content="Different content that would have produced a different title",
    )
    res = _update_live_memory(req)

    assert res["title"] == "Explicit Winning Title"

    after = _read_memory_row(db_path, "card-1")
    metadata = json.loads(after["metadata_json"])

    assert after["title"] == "Explicit Winning Title"
    assert metadata["raw_title"] == "Explicit Winning Title"

    changelog = metadata.get("changelog") or []
    latest = changelog[-1]
    assert latest["details"]["title_changed"] is True
    assert latest["details"]["content_changed"] is True


def test_title_validation_errors() -> None:
    # Empty string raises validation error
    with pytest.raises(ValidationError):
        MemoryUpdateRequest(id="card-1", title="")

    # Whitespace-only string raises validation error
    with pytest.raises(ValidationError):
        MemoryUpdateRequest(id="card-1", title="   ")

    # Over 200 characters raises validation error
    with pytest.raises(ValidationError):
        MemoryUpdateRequest(id="card-1", title="a" * 201)

    # Valid bounds: exactly 200 chars and edge whitespace stripping
    req_max = MemoryUpdateRequest(id="card-1", title="a" * 200)
    assert req_max.title == "a" * 200

    req_stripped = MemoryUpdateRequest(id="card-1", title="  Valid Padded Title  ")
    assert req_stripped.title == "Valid Padded Title"


def test_title_validation_http_422(memory_service: SQLiteMemoryService) -> None:
    client = TestClient(bhm_app.app)

    # Empty title via HTTP endpoint gives 422
    resp_empty = client.post(
        "/bhm/memory/update",
        headers=_headers(),
        json={"id": "card-1", "project": PROJECT, "title": ""},
    )
    assert resp_empty.status_code == 422

    # Whitespace title gives 422
    resp_ws = client.post(
        "/bhm/memory/update",
        headers=_headers(),
        json={"id": "card-1", "project": PROJECT, "title": "   "},
    )
    assert resp_ws.status_code == 422

    # Exceeding 200 characters gives 422
    resp_long = client.post(
        "/bhm/memory/update",
        headers=_headers(),
        json={"id": "card-1", "project": PROJECT, "title": "x" * 201},
    )
    assert resp_long.status_code == 422
