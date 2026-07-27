from __future__ import annotations

from blackholememory import app as bhm_app
from blackholememory.migration_compatibility import build_migration_preview


def test_wi14_hidden_route_and_rollback_passport():
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    assert routes["/bhm/migration/preview"].include_in_schema is False
    preview = build_migration_preview([], source_kind="fixture", source_license="MIT", project="fixture")
    assert preview["rollback"]["backup_required_before_apply"] is True
    assert preview["rollback"]["apply_performed"] is False
