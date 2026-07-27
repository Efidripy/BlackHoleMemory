from blackholememory import app as bhm_app


def test_wi09_code_fabric_route_is_hidden_and_proposal_only():
    route = next(route for route in bhm_app.app.routes if getattr(route, "path", "") == "/bhm/llm/code-fabric/plan")
    assert route.include_in_schema is False
    result = bhm_app.bhm_llm_code_fabric_plan(
        bhm_app.LLMCodeFabricPlanRequest(task_type="code_summary", project="fixture", payload={"query": "x"})
    )
    assert result["schema_version"] == "bhm.llm.code-fabric.v1"
    assert result["execution"]["model_started"] is False
    assert result["execution"]["writes_sqlite"] is False
