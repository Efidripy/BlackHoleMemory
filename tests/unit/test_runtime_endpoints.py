from __future__ import annotations

import importlib.util
from pathlib import Path

from blackholememory.runtime_endpoints import endpoint_parts
from blackholememory.runtime_endpoints import endpoint_url


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_endpoint_catalog_defaults_are_loopback_and_versioned():
    assert endpoint_url("bhm_api") == "http://127.0.0.1:8000"
    assert endpoint_url("qdrant_http") == "http://127.0.0.1:6333"
    assert endpoint_parts("lm_studio") == ("127.0.0.1", 13666)


def test_endpoint_catalog_honors_environment_overrides(monkeypatch):
    monkeypatch.setenv("BHM_PORT", "8123")
    monkeypatch.setenv("BHM_HOST", "localhost")
    assert endpoint_url("bhm_api") == "http://localhost:8123"
    monkeypatch.setenv("BHM_BASE_URL", "http://127.0.0.1:9010/custom")
    assert endpoint_url("bhm_api", "/health/ready") == "http://127.0.0.1:9010/custom/health/ready"


def test_cleanup_audit_is_read_only_and_utf8_clean():
    spec = importlib.util.spec_from_file_location("bhm_cleanup_audit", REPO_ROOT / "scripts" / "audit-bhm-cleanup.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.audit(REPO_ROOT)
    assert report["files"] > 700
    assert report["encoding"]["invalid_utf8"] == []
    assert report["encoding"]["bom_utf8"] == []
    assert report["mojibake"] == []
    assert ".src" in report["policy"]["excluded_parts"]
