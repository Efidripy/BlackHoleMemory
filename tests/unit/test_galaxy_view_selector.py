from __future__ import annotations

from pathlib import Path


STATIC = Path(__file__).resolve().parents[2] / "src" / "blackholememory" / "static"


def test_selector_exchanges_the_fragment_only_bootstrap_before_navigation() -> None:
    html = (STATIC / "galaxy-selector.html").read_text(encoding="utf-8")

    assert "Choose a BHM knowledge view" in html
    assert 'id="classicViewer"' in html
    assert 'id="atlasViewer"' in html
    assert 'viewerUrl("/bhm/galaxy/classic")' in html
    assert 'viewerUrl("/bhm/atlas")' in html
    assert 'window.location.hash.slice(1)' in html
    assert 'window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`)' in html
    assert 'window.fetch("/bhm/ui/session/exchange"' in html
    assert "bootstrap_token: bootstrapToken" in html
    assert "target.searchParams.set(\"project\", project)" in html
    assert "target.searchParams.set(\"bhm-ui-bootstrap\"" not in html


def test_atlas_is_local_read_only_preview_with_the_same_session_contract() -> None:
    html = (STATIC / "atlas.html").read_text(encoding="utf-8")

    assert "BHM Atlas Preview" in html
    assert "Read-only local preview." in html
    assert "no third-party CDN" in html
    assert 'window.fetch("/bhm/ui/session/exchange"' in html
    assert 'back.href = `/bhm/galaxy?project=${encodeURIComponent(project)}`' in html
    assert "cosmograph.app" not in html
