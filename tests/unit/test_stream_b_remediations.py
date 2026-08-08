from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from blackholememory.local_endpoint_policy import LocalEndpointError


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CRYSTALLIZER = _load("stream_b_crystallizer", "bhm_crystallize_worker.py")
RECONCILE = _load("stream_b_reconcile", "bhm_reconcile_projection.py")
CLASSIFY = _load("stream_b_classify", "bhm_classify_projection_orphans.py")
QUARANTINE = _load("stream_b_quarantine", "bhm_quarantine_projection_orphans.py")
RETENTION = _load("stream_b_retention", "bhm_retention_maintenance.py")
PROJECTION = _load("stream_b_projection", "run-bhm-projection-worker.py")
ENDPOINTS = _load("stream_b_endpoints", "bhm_runtime_endpoints.py")
STREAMABLE = _load("stream_b_streamable", "validate-bhm-p21.0-streamable-http.py")
LAUNCHER = _load("stream_b_launcher", "bhm_launcher.py")


def test_synthesis_route_is_exactly_allowlisted() -> None:
    assert CRYSTALLIZER.validate_synthesis_endpoint("/bhm/synthesis/fact-crystal") == "/bhm/synthesis/fact-crystal"
    for value in ("/bhm/memories", "https://example.com", "/bhm/synthesis/fact-crystal?next=/bhm/memories"):
        with pytest.raises(ValueError):
            CRYSTALLIZER.validate_synthesis_endpoint(value)


def test_reconcile_report_stays_under_approved_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RECONCILE, "REPORT_ROOT", tmp_path / "reports")
    target = tmp_path / "reports" / "nested" / "report.json"
    RECONCILE._write_report(target, {"ok": True})
    assert '"ok": true' in target.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="approved report root"):
        RECONCILE._write_report(tmp_path / "outside.json", {"ok": True})


@pytest.mark.parametrize("module", (CLASSIFY, QUARANTINE, RETENTION))
def test_projection_operator_reports_stay_under_approved_root(
    module: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "REPORT_ROOT", tmp_path / "reports")
    target = tmp_path / "reports" / "nested" / "report.json"
    module._write_report(target, {"ok": True})
    assert '"ok": true' in target.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="approved report root"):
        module._write_report(tmp_path / "outside.json", {"ok": True})


@pytest.mark.parametrize("module", (CLASSIFY, QUARANTINE, RETENTION))
def test_projection_operator_reports_reject_hardlink_targets(
    module: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "reports"
    monkeypatch.setattr(module, "REPORT_ROOT", root)
    target = root / "report.json"
    outside = tmp_path / "outside.json"
    root.mkdir(parents=True)
    outside.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")
    with pytest.raises(OSError, match="hardlink"):
        module._write_report(target, {"ok": True})
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_credential_bearing_endpoints_are_loopback_only() -> None:
    assert ENDPOINTS.validate_loopback_endpoint("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    with pytest.raises(ValueError, match="loopback"):
        ENDPOINTS.validate_loopback_endpoint("http://172.16.0.10:8000")
    with pytest.raises(ValueError, match="local-only"):
        PROJECTION._validate_openai_base_url("https://example.com/v1")


def test_streamable_probe_rejects_private_non_loopback_endpoint() -> None:
    with pytest.raises(LocalEndpointError, match="local-only"):
        STREAMABLE.Probe("http://172.16.0.10:8000", 1.0)


def test_launcher_mcp_config_rejects_non_loopback_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LAUNCHER, "BHM_BASE_URL", "https://example.com")
    with pytest.raises(ValueError, match="loopback"):
        LAUNCHER.mcp_config_payload()


def test_streamable_cycle_budget_has_aggregate_cap() -> None:
    assert STREAMABLE.validate_cycle_counts(100, 20) == (100, 20)
    with pytest.raises(ValueError, match="total"):
        STREAMABLE.validate_cycle_counts(100, 21)
