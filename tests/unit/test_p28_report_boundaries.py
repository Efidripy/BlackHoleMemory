from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_SCRIPTS = (
    "validate-bhm-p28-dependency-import-graph.py",
    "validate-bhm-p28-provenance-attestation.py",
    "validate-bhm-p28-provenance-boundary.py",
    "validate-bhm-p28-wi68-component-inventory.py",
    "validate-bhm-p28-wi83-git-impact.py",
    "validate-bhm-p28-wi83-watch-backpressure.py",
    "validate-bhm-p28-wi97-semantic-relevance.py",
    "validate-bhm-p28-wi99-cross-repo-history.py",
)


@pytest.mark.parametrize("script_name", REPORT_SCRIPTS)
def test_p28_report_targets_use_shared_boundary_writer(script_name: str) -> None:
    source = (REPO_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
    assert "replace_bytes_safely" in source
    assert not re.search(r"(?:args\.(?:report|output)|Path\(args\.report\))\.write_text", source)
