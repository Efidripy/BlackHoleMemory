from __future__ import annotations

from types import SimpleNamespace

from blackholememory.native_projection_parity import build_native_projection_parity_plan


class _FakeClient:
    def __init__(self, points):
        self.points = points
        self.calls = []

    def scroll(self, *, collection_name, **_kwargs):
        self.calls.append("scroll")
        return [
            SimpleNamespace(id=f"point-{index}", payload=payload)
            for index, payload in enumerate(self.points[collection_name])
        ], None


def test_native_parity_plan_is_scoped_and_read_only():
    client = _FakeClient(
        {
            "global": [{"source_id": "m1", "user_id": "u", "data": "text"}],
            "local": [{"source_id": "m2", "user_id": "u", "data": "text"}],
        }
    )

    plan = build_native_projection_parity_plan(
        client,
        [{"project": "global", "collection": "global"}, {"project": "demo", "collection": "local"}],
        expected_user_id="u",
        page_size=2,
    )

    assert plan["ok"] is True
    assert plan["mutation"] is False
    assert plan["summary"]["point_count"] == 2
    assert plan["summary"]["missing_required_projection_fields"] == 0
    assert all(item["scope"] == "collection-scoped" for item in plan["collections"])
    assert client.calls == ["scroll", "scroll"]


def test_native_parity_plan_reports_scoped_backfill_target_without_mutation():
    client = _FakeClient({"local": [{"source_id": "m1", "user_id": "", "data": "text"}]})

    plan = build_native_projection_parity_plan(
        client,
        [{"project": "demo", "collection": "local"}],
        expected_user_id="u",
    )

    assert plan["ok"] is True
    assert plan["mutation"] is False
    assert plan["summary"]["missing_user_scope"] == 1
    assert plan["apply_boundary"]["required_before_apply"] is True

