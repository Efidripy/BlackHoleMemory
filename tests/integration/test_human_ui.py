from __future__ import annotations

from pathlib import Path

from blackholememory import app as bhm_app
from blackholememory.human_ui_bridge import build_human_ui_bridge_preview


def test_ui_boot_report_is_redacted_and_navigation_is_same_origin():
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    assert routes["/bhm/ui/boot-report"].path == "/bhm/ui/boot-report"
    static_dir = Path(__file__).resolve().parents[2] / "src" / "blackholememory" / "static"
    index = (static_dir / "index.html").read_text(encoding="utf-8")
    links = (static_dir / "links.html").read_text(encoding="utf-8")
    assert 'href="/bhm/galaxy"' in index
    assert 'href="/bhm/galaxy"' in links
    assert "http://127.0.0.1:8000/bhm/galaxy" not in index
    assert "http://127.0.0.1:8000/bhm/galaxy" not in links
    assert 'id="memoryRecordsActive"' in index
    assert 'id="architectureDecisionsActive"' in index
    for element_id in (
        "coreBlockState",
        "mcpBlockState",
        "uiSessionBlockState",
        "agentSessionBlockState",
        "queueBlockState",
        "storageBlockState",
        "healthObservedAt",
    ):
        assert f'id="{element_id}"' in index
    assert "MCP pipe pool" not in index
    assert "not configured" in index
    assert "architecture laws" not in index.lower()
    assert "crystals_total" not in index
    assert "Fact crystals total" not in links
    assert "crystals_total" not in links


def test_hidden_route_and_existing_galaxy_surface():
    routes = {str(route.path): route for route in bhm_app.app.routes if hasattr(route, "path")}
    assert routes["/bhm/human-ui/preview"].include_in_schema is False
    assert routes["/bhm/galaxy"].path == "/bhm/galaxy"
    preview = build_human_ui_bridge_preview(project="fixture", nodes=[], links=[], context_packet={"token_usage": 0, "max_tokens": 1200})
    assert "/bhm/galaxy" in preview["surfaces"]
    assert preview["checks"]["obsidian_optional"] is True


def test_dashboard_and_galaxy_expose_explicit_runtime_context_blocks():
    static_dir = Path(__file__).resolve().parents[2] / "src" / "blackholememory" / "static"
    index = (static_dir / "index.html").read_text(encoding="utf-8")
    galaxy = (static_dir / "galaxy.html").read_text(encoding="utf-8")
    assert 'id="systemBlocksTitle"' in index
    assert 'id="systemContext"' in galaxy
    for element_id in ("contextCore", "contextStorage", "contextMcp", "contextProjection", "contextObservedAt"):
        assert f'id="{element_id}"' in galaxy
    assert "SQLite authority" in galaxy
    assert "Streamable HTTP sessions" in galaxy
