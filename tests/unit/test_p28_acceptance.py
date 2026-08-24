from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "validate-bhm-capability-acceptance.py"
_CROSSWALK = (
    Path(__file__).resolve().parents[2]
    / ".docs"
    / "config"
    / "cbm-bhm-capability-crosswalk.json"
)
_SPEC = importlib.util.spec_from_file_location("validate_bhm_p28_acceptance", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
build_report = _MODULE.build_report
validate_shape = _MODULE._validate_crosswalk_shape
tracked_source_files = _MODULE._tracked_source_files


@pytest.mark.skipif(
    not _CROSSWALK.is_file(),
    reason="P28 crosswalk is local release evidence and is absent from public checkout",
)
def test_p28_acceptance_report_is_read_only_and_truthful() -> None:
    repo = Path(__file__).resolve().parents[2]
    report = build_report(repo)
    assert report["ok"] is True
    assert report["acceptance_ready"] is True
    assert report["acceptance_semantics"] == "local_product"
    assert report["local_product_ready"] is True
    assert report["open_capabilities"] == [
        "CBM-CAP-05",
        "CBM-CAP-06",
        "CBM-CAP-07",
        "CBM-CAP-08",
        "CBM-CAP-09",
        "CBM-CAP-10",
        "CBM-CAP-11",
    ]
    for field in (
        "external_" + "certification_ready",
        "external_" + "open_capabilities",
        "external_" + "authority_gates",
    ):
        assert field not in report
    report_text = json.dumps(report, ensure_ascii=False)
    assert all(f"CBM-{'CAP'}-{index:02d}" not in report_text for index in (12, 13, 14))
    assert report["source_boundary"]["clean"] is True
    assert report["execution"]["writes_worktree"] is False
    assert report["evidence_boundary"]["clean"] is True


def test_crosswalk_shape_rejects_traversal_secrets_and_duplicate_ids(tmp_path: Path) -> None:
    (tmp_path / "evidence.json").write_text("{}", encoding="utf-8")
    capabilities = [
        {"id": "CAP-1", "name": "one", "evidence": ["evidence.json", "../escape.md"]},
        {"id": "CAP-1", "name": "duplicate", "evidence": [".env"]},
    ]

    result = validate_shape(tmp_path, capabilities)

    assert result["checked"] == 3
    assert result["safe"] == 1
    assert any("duplicate capability id" in item for item in result["failures"])
    assert any("unsafe evidence path" in item for item in result["failures"])
    assert any("blocked boundary" in item for item in result["failures"])


def test_tracked_source_probe_is_bounded_and_fails_closed(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    def timeout(*_args, **kwargs):
        calls.update(kwargs)
        raise subprocess.TimeoutExpired(kwargs.get("args", "git"), _MODULE.GIT_PROBE_TIMEOUT_SECONDS)

    monkeypatch.setattr(_MODULE.subprocess, "run", timeout)
    rows, failure = tracked_source_files(tmp_path)

    assert rows == ["git-check-unavailable"]
    assert failure == "git source-boundary check unavailable"
    assert calls["timeout"] == _MODULE.GIT_PROBE_TIMEOUT_SECONDS
