from __future__ import annotations

import re
import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_SCRIPTS = (
    "validate-bhm-dependency-import-graph.py",
    "validate-bhm-provenance-attestation.py",
    "validate-bhm-provenance-boundary.py",
    "validate-bhm-component-inventory.py",
    "validate-bhm-git-impact.py",
    "validate-bhm-watch-backpressure.py",
    "validate-bhm-semantic-relevance.py",
    "validate-bhm-cross-repository-history.py",
    "bhm-capability-router.py",
    "bhm-code-graph-query.py",
    "bhm-code-graph.py",
    "bhm-conventions.py",
)


@pytest.mark.parametrize("script_name", REPORT_SCRIPTS)
def test_p28_report_targets_use_shared_boundary_writer(script_name: str) -> None:
    source = (REPO_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
    assert "replace_bytes_safely" in source
    assert not re.search(r"(?:args\.(?:report|output)|Path\(args\.report\))\.write_text", source)


def test_code_graph_report_writer_rejects_dangling_symlink(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("bhm_code_graph_report_test", REPO_ROOT / "scripts" / "bhm-code-graph.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "report.json"
    outside = tmp_path / "outside.json"
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")

    with pytest.raises(OSError):
        module._json({"ok": True}, str(target))
    assert not outside.exists()
