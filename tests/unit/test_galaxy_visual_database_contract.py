from __future__ import annotations

from pathlib import Path


GALAXY_HTML = Path(__file__).resolve().parents[2] / "src/blackholememory/static/galaxy.html"


def _html() -> str:
    return GALAXY_HTML.read_text(encoding="utf-8")


def test_galaxy_exposes_newest_first_amounts_and_true_all_mode() -> None:
    html = _html()

    assert '<select id="limit">' in html
    for value in ("50", "200", "500", "1000", "all"):
        assert f'<option value="{value}"' in html
    assert "«Все» не имеет скрытого лимита" in html
    assert 'query.set("limit", controls.limit.value || "200")' in html
    assert 'controls.limit.addEventListener("change", () => loadGalaxy(true))' in html
    assert 'max="500"' not in html


def test_galaxy_project_scope_and_depth_are_primary_controls() -> None:
    html = _html()

    assert 'id="project" type="text"' in html
    assert 'placeholder="Все проекты"' in html
    assert 'id="projectSuggestions"' in html
    assert 'id="focusMode"' in html
    assert 'id="hopDepth" type="range" min="1" max="3"' in html
    assert 'query.set("hop_depth", String(state.hopDepth))' in html
    assert "function hopScopedIds" in html
    assert 'if (!(controls.project.value || "").trim()) {' in html
    assert "closeProjectSuggestions();\n          loadGalaxy(true);" in html


def test_galaxy_discloses_returned_and_total_authoritative_records() -> None:
    html = _html()

    assert 'id="shownCount"' in html
    assert "summary.returned_memory_count" in html
    assert "summary.total_memory_count" in html
    assert "Loaded ${returned} of ${total} memories" in html


def test_galaxy_displays_global_authoritative_graph_totals_separately() -> None:
    html = _html()

    assert 'id="globalNodeCount"' in html
    assert 'id="globalLinkCount"' in html
    assert 'id="globalStatsStatus"' in html
    assert 'bhmFetch("/bhm/galaxy/stats"' in html
    assert "SQLite-authoritative" in html


def test_galaxy_operator_surfaces_are_removed_from_the_visible_sidebar() -> None:
    html = _html()

    assert "[hidden] { display: none !important; }" in html
    assert '<section class="galaxy-intro" aria-labelledby="galaxyIntroTitle" hidden>' in html
    assert '<details id="advancedFiltersGroup" class="advanced-panel" hidden>' in html
    assert '<details id="operatorDiagnosticsGroup" class="advanced-panel" hidden>' in html
    assert '<a class="link-btn secondary" href="/" target="_self" hidden>' in html
    assert "void refreshRuntimeQuality();" not in html
    assert "void refreshMcpPanel();" not in html
    assert "void refreshCbmParity();" not in html


def test_galaxy_refreshes_when_live_pulse_targets_a_new_node() -> None:
    html = _html()

    assert "const visible = triggerMemoryPulse(payload.node_id);" in html
    assert "if (!visible && !state.pulseReloadTimer)" in html
    assert "void loadGalaxy(false);" in html
