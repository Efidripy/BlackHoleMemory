from blackholememory import app as bhm_app


def test_wi10_factory_route_is_hidden_and_review_only():
    route = next(route for route in bhm_app.app.routes if getattr(route, "path", "") == "/bhm/factories/preview")
    assert route.include_in_schema is False
    result = bhm_app.bhm_factories_preview(bhm_app.FactoryIntegrationPreviewRequest(project="fixture", changed_paths=["src/a.py"], code_items=[{"path": "src/a.py", "test_paths": ["tests/test_a.py"]}], task_items=[{"task_id": "t1", "files_touched": ["src/a.py"]}]))
    assert result["schema_version"] == "bhm.factory-integration.v1"
    assert result["execution"]["auto_apply"] is False
