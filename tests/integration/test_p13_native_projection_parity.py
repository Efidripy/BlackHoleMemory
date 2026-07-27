from __future__ import annotations

from blackholememory.config import settings
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.mem0_adapter import global_collection_name
from blackholememory.mem0_adapter import local_collection_name
from blackholememory.native_projection_parity import build_native_projection_parity_plan
from blackholememory.project_registry import get_default_project_registry


def test_live_active_collections_are_native_green_without_backfill_apply():
    scopes = [{"project": "global", "collection": global_collection_name()}]
    scopes.extend(
        {"project": item["id"], "collection": local_collection_name(item["id"])}
        for item in get_default_project_registry().report()["projects"]
    )
    plan = build_native_projection_parity_plan(
        get_qdrant_client(),
        scopes,
        expected_user_id=str(settings.mem0_user_id),
        page_size=128,
    )

    assert plan["ok"] is True
    assert plan["mutation"] is False
    assert plan["summary"]["collection_count"] == 3
    assert plan["summary"]["point_count"] == sum(item["point_count"] for item in plan["collections"])
    assert plan["summary"]["point_count"] >= 1022
    assert plan["summary"]["missing_required_projection_fields"] == 0
    assert plan["summary"]["mismatched_user_scope"] == 0
    assert plan["summary"]["missing_source_id"] == 0
    assert plan["apply_boundary"]["required_before_apply"] is False
