from __future__ import annotations

from pathlib import Path

from scripts.validate_doc_links import validate


def test_doc_link_gate_checks_active_markdown_and_skips_historical_trees(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "target.md").write_text("# Target\n", encoding="utf-8")
    (tmp_path / "docs" / "active.md").write_text(
        "[target](target.md) [external](https://example.test)\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "ops").mkdir()
    (tmp_path / "docs" / "ops" / "historical.md").write_text(
        "[missing](missing-receipt.md)\n",
        encoding="utf-8",
    )

    report = validate(tmp_path)

    assert report["ok"] is True
    assert report["files_checked"] == 2
    assert report["links_checked"] == 1


def test_doc_link_gate_reports_missing_active_target(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("[missing](docs/nope.md)\n", encoding="utf-8")

    report = validate(tmp_path)

    assert report["ok"] is False
    assert report["missing"] == [{"source": "README.md", "target": "docs/nope.md"}]


def test_doc_link_gate_checks_local_heading_anchors(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "[ok](#local-heading) [bad](#missing-heading)\n\n## Local heading\n",
        encoding="utf-8",
    )

    report = validate(tmp_path)

    assert report["ok"] is False
    assert report["links_checked"] == 2
    assert report["anchors_checked"] == 2
    assert report["missing"] == [
        {"source": "README.md", "target": "#missing-heading", "reason": "missing_anchor"}
    ]
