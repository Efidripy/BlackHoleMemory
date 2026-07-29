from __future__ import annotations

import importlib.util
import json
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "validate-bhm-p28-acceptance.py"
_SPEC = importlib.util.spec_from_file_location("validate_bhm_p28_acceptance", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
build_report = _MODULE.build_report
validate_shape = _MODULE._validate_crosswalk_shape


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
