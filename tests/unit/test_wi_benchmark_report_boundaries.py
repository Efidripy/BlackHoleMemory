from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.filesystem_boundaries import replace_bytes_safely


ROOT = Path(__file__).resolve().parents[2]

REPORT_WRITER_SCRIPTS = (
    "benchmark-bhm-wi01-repository-index.py",
    "benchmark-bhm-wi02-code-graph.py",
    "benchmark-bhm-wi03-code-graph-query.py",
    "benchmark-bhm-wi04-conventions.py",
    "benchmark-bhm-wi05-session-capture.py",
    "benchmark-bhm-wi06-memory-graph.py",
    "benchmark-bhm-wi07-task-graph.py",
    "benchmark-bhm-wi08-unified-context.py",
    "benchmark-bhm-wi09-llm-code-fabric.py",
    "benchmark-bhm-wi10-factories.py",
    "benchmark-bhm-wi11-unified-mcp.py",
    "benchmark-bhm-wi12-human-ui.py",
    "benchmark-bhm-wi13-capability-router.py",
    "benchmark-bhm-wi14-migration.py",
    "benchmark-bhm-wi15-security.py",
    "benchmark-bhm-wi17-product-value.py",
    "benchmark-bhm-wi143-semantic-relevance.py",
    "benchmark-bhm-wi150-parser-families.py",
    "benchmark-bhm-wi153-parser-families.py",
    "benchmark-bhm-wi158-bitbake-parser.py",
    "benchmark-bhm-wi162-github-actions.py",
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
