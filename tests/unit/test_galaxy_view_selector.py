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
    assert "window.location.hash.slice(1)" in html
    assert 'window.fetch("/bhm/ui/session/exchange"' in html
    assert "bootstrap_token: bootstrapToken" in html
    assert 'target.searchParams.set("project", project)' in html
    assert 'target.searchParams.set("bhm-ui-bootstrap"' not in html


def test_atlas_is_a_local_g6_knowledge_map_with_the_same_session_contract() -> None:
    html = (STATIC / "atlas.html").read_text(encoding="utf-8")

    assert "BHM Atlas · G6 3D Knowledge Map" in html
    assert "BHM Atlas 3D knowledge map" in html
    assert 'src="/static/g6.min.js' in html
    assert 'src="/static/g6-extension-3d.min.js' in html
    assert "const G6=window.G6" in html
    assert "Atlas3D=window.G6Extension3D" in html
    assert "new G6.Graph" in html
    assert "renderer:Atlas3D.renderer" in html
    assert 'type:"bhm-atlas-sphere"' in html
    assert 'type:"bhm-atlas-line3d"' in html
    assert 'type:"bhm-atlas-observe-3d"' in html
    assert 'projectionMode:"perspective"' in html
    assert "territoryCoordinates" in html
    assert 'id="atlasViewport"' in html
    assert 'id="pulseBtn"' in html
    assert 'id="fitBtn"' in html
    assert 'id="fullscreenBtn"' in html
    assert 'id="ambientMotion"' in html
    assert 'return "/bhm/galaxy/data?"+query.toString()' in html
    assert "Persisted relations only" in html
    assert "rawPayload:null" in html
    assert "state.rawPayload=payload" in html
    assert "renderData(state.rawPayload)" in html
    assert "3d-force-graph" not in html
    assert 'id="sessionGate"' in html
    assert "sessionGateEl.hidden=false" in html
    assert "Open Atlas through BHM Launcher" in html
    assert 'window.fetch("/bhm/ui/session/exchange"' in html
    assert 'state.uiSessionReady=bootstrapUiSession();state.uiSessionReady.then(()=>{syncQuery();return loadAtlas()})' in html
    assert 'controls.project.value=new URLSearchParams(window.location.search).get("project")||"";syncQuery();state.uiSessionReady=bootstrapUiSession()' not in html
    assert 'document.getElementById("backToSelector").href="/bhm/galaxy"+suffix' in html
    assert 'type:"click-select"' in html
    assert 'type:"hover-activate"' in html
    assert 'type:"bhm-atlas-pan-3d"' in html
    assert "Atlas3D.DragCanvas3D" in html
    assert 'type:"bhm-atlas-observe-3d",mode:"orbiting",trigger:["Alt"]' in html
    assert 'type:"drag-element",trigger:[],dropEffect:"none",animation:true,shadow:true' in html
    assert "onFinish:ids=>ids.forEach(bounceNode)" in html
    assert 'graph.on("node:click",event=>selectNode(event.target.id))' in html
    assert "nearestPointerNode" in html
    assert "graph.getCanvasByClient" in html
    assert "graph.getClientByCanvas" in html
    assert "graph.getElementRenderBounds" in html
    assert "setPointerCapture" in html
    assert "pointerdown" in html
    assert "pointermove" in html
    assert 'materialType:"phong"' in html
    assert "startAmbientMotion()" in html
    assert "function pulseAtlas()" in html
    assert "function toggleFullscreen()" in html
    assert "document.documentElement.requestFullscreen()" in html
    assert "autoResize:true" in html
    assert "state.graph.resize(bounds.width,bounds.height)" in html
    assert "state.graph.setSize(bounds.width,bounds.height);fitMap()" not in html
    assert "webglcontextrestored" in html
    assert "cosmograph.app" not in html
