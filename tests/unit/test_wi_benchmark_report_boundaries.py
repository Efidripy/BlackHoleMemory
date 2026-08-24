from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.filesystem_boundaries import replace_bytes_safely


ROOT = Path(__file__).resolve().parents[2]

REPORT_WRITER_SCRIPTS = (
    "benchmark-bhm-repository-index.py",
    "benchmark-bhm-code-graph.py",
    "benchmark-bhm-code-graph-query.py",
    "benchmark-bhm-conventions.py",
    "benchmark-bhm-session-capture.py",
    "benchmark-bhm-memory-graph.py",
    "benchmark-bhm-task-graph.py",
    "benchmark-bhm-unified-context.py",
    "benchmark-bhm-llm-code-fabric.py",
    "benchmark-bhm-factories.py",
    "benchmark-bhm-unified-mcp.py",
    "benchmark-bhm-human-ui.py",
    "benchmark-bhm-capability-router.py",
    "benchmark-bhm-migration.py",
    "benchmark-bhm-security.py",
    "benchmark-bhm-product-value.py",
    "rebuild-bhm-p28-wi68-inventory.py",
)


@pytest.mark.parametrize("filename", REPORT_WRITER_SCRIPTS)
def test_wi_report_scripts_use_boundary_aware_replacement(filename: str) -> None:
    source = (ROOT / "scripts" / filename).read_text(encoding="utf-8")
    assert "from blackholememory.filesystem_boundaries import replace_bytes_safely" in source
    assert "replace_bytes_safely(" in source
    report_lines = [line for line in source.splitlines() if "write_text(" in line and ("rendered" in line or "payload" in line)]
    assert report_lines == []


def test_wi_report_boundary_rejects_hardlink_target(tmp_path: Path) -> None:
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
