from __future__ import annotations

import pytest
from fastapi import HTTPException

from blackholememory import app as bhm_app


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (
            lambda: bhm_app._schema_upgrade_all(bhm_app.SchemaUpgradeAllRequest()),
            "schema_upgrade_scope_required",
        ),
        (
            lambda: bhm_app._reindex_memory_metadata(),
            "reindex_scope_required",
        ),
        (
            lambda: bhm_app._entity_catalog_rebuild(),
            "entity_catalog_scope_required",
        ),
        (
            lambda: bhm_app._relation_apply_suggestions(bhm_app.RelationApplySuggestionsRequest()),
            "relation_apply_scope_required",
        ),
        (
            lambda: bhm_app._relation_prune_low_quality(bhm_app.RelationPruneLowQualityRequest()),
            "relation_prune_scope_required",
        ),
        (
            lambda: bhm_app._overlap_cleanup_apply(bhm_app.OverlapCleanupApplyRequest()),
            "overlap_cleanup_scope_required",
        ),
    ],
)
def test_global_maintenance_requires_explicit_scope(call, code: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        call()

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == code


def test_global_maintenance_accepts_explicit_aggregate(monkeypatch) -> None:
    monkeypatch.setattr(bhm_app, "_load_live_memories", lambda: [])
    monkeypatch.setattr(bhm_app, "_save_live_memories", lambda _items: None)
    monkeypatch.setattr(bhm_app, "_load_memory_links", lambda: [])
    monkeypatch.setattr(bhm_app, "_save_memory_links", lambda _items: None)
    monkeypatch.setattr(bhm_app, "_load_entity_catalogs", lambda: [])
    monkeypatch.setattr(bhm_app, "_save_entity_catalogs", lambda _items: None)

    assert bhm_app._schema_upgrade_all(bhm_app.SchemaUpgradeAllRequest(aggregate=True))["project"] is None
    assert bhm_app._reindex_memory_metadata(aggregate=True)["project"] is None
    assert bhm_app._entity_catalog_rebuild(aggregate=True)["project"] is None
    assert bhm_app._relation_prune_low_quality(
        bhm_app.RelationPruneLowQualityRequest(aggregate=True)
    )["project"] is None
