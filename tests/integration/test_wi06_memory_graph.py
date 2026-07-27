from blackholememory import app as bhm_app


def test_wi06_memory_graph_routes_are_hidden_and_share_one_database_path():
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    for path in ("/bhm/memory-graph/query", "/bhm/memory-graph/explain"):
        assert path in routes
        assert routes[path].include_in_schema is False
    assert bhm_app._memory_graph_database_path().name == "memories.sqlite3"
