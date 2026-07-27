from __future__ import annotations

from pathlib import Path


GALAXY_HTML = Path(__file__).resolve().parents[2] / "src/blackholememory/static/galaxy.html"


def test_galaxy_mobile_overlay_contract_is_present():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert 'id="mobileSidebarOpen"' in html
    assert 'id="mobileDetailOpen"' in html
    assert 'id="mobileScrim"' in html
    assert 'aria-controls="galaxySidebar"' in html
    assert 'aria-controls="detailPanel"' in html
    assert "body.mobile-sidebar-open .panel.sidebar" in html
    assert "body.mobile-detail-open .panel.right" in html
    assert "function handleMobilePanelKeydown" in html
    assert "@media (max-width: 980px)" in html


def test_galaxy_dynamic_status_is_announced():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert '<div id="status" class="status" role="status" aria-live="polite">' in html
    assert "prefers-reduced-motion: reduce" in html


def test_galaxy_keyboard_and_non_canvas_summary_contract_is_present():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert '<h1 class="sr-only">BHM Galaxy Viewer</h1>' in html
    assert 'id="graph" role="img" aria-labelledby="graphSummaryTitle"' in html
    assert '<details id="graphSummary" class="graph-summary">' in html
    assert 'id="graphSummaryText" class="graph-summary-text" role="status" aria-live="polite"' in html
    assert 'class="legend-item" type="button"' in html
    assert 'aria-pressed="false"' in html
    assert "function updateGraphSummary" in html
    assert "function selectGraphNode" in html
    assert "item.setAttribute(\"aria-pressed\", String(active));" in html


def test_galaxy_runtime_quality_contract_is_present():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert 'id="qualityCard" class="quality-card"' in html
    assert 'id="qualitySummary" class="quality-summary" role="status" aria-live="polite"' in html
    assert "function graphQualitySnapshot" in html
    assert "function renderRuntimeQuality" in html
    assert "function refreshRuntimeQuality" in html
    assert 'bhmFetch("/bhm/health/slo", { cache: "no-store" })' in html
    assert 'bhmFetch("/health/cutover", { cache: "no-store" })' in html
    assert "nodeMetadata.metadata" in html


def test_galaxy_global_bhm_cbm_domain_selector_contract_is_present():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert 'id="galaxyDomain"' in html
    assert 'value="memory"' in html
    assert 'value="code"' in html
    assert 'query.set("domain", controls.domain.value || "all")' in html
    assert 'controls.domain.addEventListener("change", () => loadGalaxy(true))' in html


def test_galaxy_project_resolution_is_fail_safe_for_partial_link_endpoints():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert 'function nodeProject(node)' in html
    assert 'meta.project || (node && node.project)' in html
    assert 'nodes.map(node => nodeProject(node))' in html


def test_galaxy_diagnostics_contract_is_present():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert 'id="diagnosticsPanel" class="diagnostics-panel"' in html
    assert 'id="diagnosticQuery" type="search"' in html
    assert 'id="retrievalExplainList" class="diagnostic-list"' in html
    assert 'id="reconciliationList" class="diagnostic-list"' in html
    assert 'postDiagnosticsJson("/bhm/retrieval/explain"' in html
    assert "function renderRetrievalExplanation" in html
    assert "function renderReconciliationView" in html
    assert "function refreshReconciliation" in html
    assert "projection_pending" in html


def test_galaxy_slo_and_growth_alert_contract_is_present():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert 'id="sloDashboard" class="slo-dashboard"' in html
    assert 'id="sloCheckList" class="slo-check-list"' in html
    assert 'id="growthAlert" class="growth-alert"' in html
    assert "function renderSloDashboard" in html
    assert "function captureGrowthSnapshot" in html
    assert "function renderGrowthAlerts" in html
    assert "bhm-galaxy-growth-snapshot-v1" in html
    assert "projection backlog" in html


def test_galaxy_cbm_metadata_search_paginates_against_public_code_dsl():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert 'id="cbmCodeSearchMoreBtn"' in html
    assert 'search_mode: cbmCodeSearchModeEl.value' in html
    assert 'offset: cbmCodeSearchOffset' in html
    assert 'Number.isInteger(payload.next_offset)' in html
    assert 'cbmCodeSearchMoreBtn.addEventListener("click"' in html
    assert "raw source disabled" in html


def test_galaxy_cbm_graph_query_is_bounded_cancellable_and_metadata_only():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert 'id="cbmGraphQueryPanel"' in html
    assert 'id="cbmGraphQueryCancelBtn"' in html
    assert 'id="cbmGraphQueryPreset"' in html
    assert 'file_function_calls' in html
    assert 'id="uiSessionRetryBtn"' in html
    assert 'uiSessionRetryBtn?.addEventListener("click"' in html
    assert 'window.location.reload();' in html
    assert 'operation: "graph_query"' in html
    assert 'signal: cbmGraphQueryController.signal' in html
    assert 'window.setTimeout(() => cbmGraphQueryController?.abort(), 5000)' in html
    assert 'cbmGraphQueryCancelBtn.addEventListener("click", cancelCbmGraphQuery)' in html
    assert "SQLite-authoritative · metadata-only · raw source disabled" in html
    assert "Graph query cancelled or timed out" in html
    assert 'payload.query_plan?.pattern?.two_hop' in html


def test_galaxy_query_quality_receipt_is_progressively_disclosed_from_server_fields():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert 'id="cbmGraphQueryQuality" class="evidence-receipt"' in html
    assert 'id="cbmGraphQueryQualityText" class="evidence-receipt-text"' in html
    assert "function renderGraphQueryQualityReceipt" in html
    assert "payload?.quality_receipt" in html
    assert "coverage.node_bucket" in html
    assert "coverage.edge_bucket" in html
    assert "histograms.unresolved_edge_count" in html
    assert "bounds.stale_snapshot" in html
    assert "bounds.truncated" in html
    assert "provenance.review_required" in html
    assert "receipt.evidence_digest" in html
    assert "renderGraphQueryQualityReceipt({})" in html



def test_galaxy_cbm_coverage_card_uses_server_metadata_and_digests():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert 'id="cbmParityPanel" class="mcp-panel"' in html
    assert 'bhmFetch("/bhm/ui/code-tools"' in html
    assert 'request("schema")' in html
    assert 'request("coverage")' in html
    assert '["Coverage complete", coverageData.complete === true ? "yes" : coverageData.complete === false ? "no" : "unknown"]' in html
    assert '["Registry digest", String(schema.parser_registry_digest || "unknown")]' in html
    assert '["Inventory digest", String(schema.language_inventory_digest || "unknown")]' in html
    assert '["Contract digest", String(schema.contract_digest || coverage.contract_digest || "unknown")]' in html
    assert 'coverageData.parsed || 0' not in html
    assert 'coverageData.file_count || 0' not in html
    assert 'raw source is never returned' in html


def test_galaxy_cbm_surfaces_have_progressive_evidence_receipts():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert 'id="cbmCoverageEvidence" class="evidence-receipt"' in html
    assert 'id="cbmCoverageEvidenceText"' in html
    assert 'id="cbmCodeSearchReceipt" class="evidence-receipt"' in html
    assert 'id="cbmGraphQueryReceipt" class="evidence-receipt"' in html
    assert "function renderEvidenceReceipt" in html
    assert "function boundedReceiptFields" in html
    assert 'renderEvidenceReceipt(cbmCoverageEvidenceTextEl' in html
    assert 'renderEvidenceReceipt(cbmCodeSearchReceiptEl' in html
    assert 'renderEvidenceReceipt(cbmGraphQueryReceiptEl' in html
    assert '["Raw source", "disabled"]' in html
    assert 'Writes", "disabled"' in html


def test_galaxy_mcp_session_lease_receipt_is_bounded_and_cancelable():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert 'id="mcpLeaseEvidence" class="evidence-receipt"' in html
    assert 'id="mcpLeaseEvidenceText"' in html
    assert 'id="mcpLeaseCancelBtn"' in html
    assert '"/bhm/mcp/http/status"' in html
    assert 'lease_remaining_seconds' in html
    assert 'contract_drift_count' in html
    assert "function cancelMcpPanelRefresh" in html
    assert "mcpPanelController = new AbortController()" in html
    assert "refresh cancelled" in html
    assert "raw source disabled" in html
    assert "Math.min(...leaseSeconds)" in html
    assert "slice(0, 8)" in html


def test_galaxy_mcp_reconnect_receipt_is_progressive_and_source_free():
    html = GALAXY_HTML.read_text(encoding="utf-8")

    assert 'id="mcpReconnectEvidence" class="evidence-receipt"' in html
    assert 'id="mcpReconnectEvidenceText"' in html
    assert "function renderMcpReconnectReceipt" in html
    assert "panel.reconnect_receipt" in html
    assert 'bounded.schema_version' in html
    assert 'bounded.status' in html
    assert 'bounded.state' in html
    assert 'bounded.action' in html
    assert 'bounded.native_client_attach_proven === true' in html
    assert 'bounded.deterministic_digest' in html
    assert 'bounded.gaps.slice(0, 4)' in html
    assert '"not proven by UI"' in html
    assert '"read-only · no live writes"' in html
    assert 'session_id' not in html
    assert 'access_token' not in html
