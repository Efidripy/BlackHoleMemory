from datetime import datetime, timezone

from blackholememory import app as bhm_app


def test_wi05_hidden_session_capture_route_is_read_only(monkeypatch):
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

    class FakeObservationStore:
        def load(self, **_kwargs):
            return [
                {
                    "eventId": "evt-route",
                    "sessionId": "sess-route",
                    "project": "blackholememory",
                    "hookType": "tool.complete",
                    "timestamp": now.isoformat().replace("+00:00", "Z"),
                    "data": {"result": "ok"},
                    "recordSha256": "d" * 64,
                }
            ]

    monkeypatch.setattr(bhm_app, "_observation_store", lambda: FakeObservationStore())
    monkeypatch.setattr(bhm_app, "_load_session_records", lambda: [{"id": "sess-rec", "project": "blackholememory", "session_id": "sess-route", "title": "Route", "metadata": {"session_id": "sess-route"}}])
    monkeypatch.setattr(bhm_app, "_load_live_memories", lambda: [{"source_id": "mem-route", "project": "blackholememory", "memory_type": "fact", "content": "safe fact", "updated_at": now.isoformat().replace("+00:00", "Z")}])

    response = bhm_app.bhm_session_capture_preview(
        bhm_app.SessionCapturePreviewRequest(project="blackholememory", session_id="sess-route", disclosure="audit")
    )
    assert response["schema_version"] == "bhm.session-capture.v1"
    assert response["execution"]["writes_sqlite"] is False
    assert response["packet"]["provenance"]["session_id"] == "sess-route"
    route = next(route for route in bhm_app.app.routes if getattr(route, "path", "") == "/bhm/session-capture/preview")
    assert route.include_in_schema is False
