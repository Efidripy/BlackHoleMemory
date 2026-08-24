from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.filesystem_boundaries import replace_bytes_safely


ROOT = Path(__file__).resolve().parents[2]

REPORT_WRITER_SCRIPTS = (
    "validate-bhm-source-passport.py",
    "validate-bhm-repository-index.py",
    "validate-bhm-code-graph.py",
    "validate-bhm-code-graph-query.py",
    "validate-bhm-conventions.py",
    "validate-bhm-session-capture.py",
    "validate-bhm-memory-graph.py",
    "validate-bhm-task-graph.py",
    "validate-bhm-unified-context.py",
    "validate-bhm-llm-code-fabric.py",
    "validate-bhm-factories.py",
    "validate-bhm-unified-mcp.py",
    "validate-bhm-human-ui.py",
    "validate-bhm-capability-router.py",
    "validate-bhm-migration.py",
    "validate-bhm-security.py",
    "validate-bhm-final-acceptance.py",
)


@pytest.mark.parametrize("filename", REPORT_WRITER_SCRIPTS)
def test_wi_validator_scripts_use_boundary_aware_replacement(filename: str) -> None:
    source = (ROOT / "scripts" / filename).read_text(encoding="utf-8")
    assert "from blackholememory.filesystem_boundaries import replace_bytes_safely" in source
    assert "replace_bytes_safely(" in source
    report_lines = [line for line in source.splitlines() if "write_text(" in line and "rendered" in line]
    assert report_lines == []


def test_wi_validator_report_boundary_rejects_hardlink_target(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        replace_bytes_safely(target, b"replacement")
    assert outside.read_text(encoding="utf-8") == "sentinel"
