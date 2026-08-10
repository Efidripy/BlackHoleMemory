from __future__ import annotations

from fastapi.testclient import TestClient

from blackholememory import app as bhm_app


def test_adr_supersede_accepts_json_body(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_supersede(project: str, old_id: str, new_id: str) -> dict:
        captured.update(project=project, old_id=old_id, new_id=new_id)
        return {
            "old": {"id": old_id, "project": project},
            "new": {"id": new_id, "project": project},
        }

    monkeypatch.setattr(bhm_app, "_adr_supersede", fake_supersede)
    monkeypatch.setattr(bhm_app, "_serialize_adr_record", lambda record: record)

    response = TestClient(bhm_app.app).post(
        "/bhm/adr/supersede",
        json={"project": "jmaka", "old_id": "adr-old", "new_id": "adr-new"},
    )

    assert response.status_code == 200
    assert captured == {"project": "jmaka", "old_id": "adr-old", "new_id": "adr-new"}
    assert response.json()["success"] is True
