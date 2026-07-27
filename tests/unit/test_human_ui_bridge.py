from __future__ import annotations

from blackholememory.human_ui_bridge import build_human_ui_bridge_preview
from blackholememory.human_ui_bridge import verify_human_ui_bridge_digest


def test_human_ui_bridge_is_bounded_and_explainable():
    preview = build_human_ui_bridge_preview(
        project="fixture",
        nodes=[{"id": "a", "label": "A", "project": "fixture", "source_ref": "docs/a.md", "stale": True, "quarantined": False}],
        links=[],
        selected_id="a",
        context_packet={"token_usage": 10, "max_tokens": 100},
    )
    assert verify_human_ui_bridge_digest(preview)
    assert preview["checks"]["selected_provenance_explainable"] is True
    assert preview["checks"]["stale_quarantine_visible"] is True
    assert preview["execution"]["authority_written"] is False


def test_obsidian_bridge_requires_marker_and_reports_checksum_conflict():
    base = build_human_ui_bridge_preview(
        project="fixture",
        obsidian_export=[{"entity_id": "a", "title": "A", "content": "safe", "source_ref": "docs/a.md", "confidence": 0.8}],
        snapshot_id="s1",
        generated_at="2026-07-16T00:00:00Z",
    )
    note = base["obsidian_export"]["notes"][0]
    accepted = build_human_ui_bridge_preview(project="fixture", obsidian_import=[{"entity_id": "a", "title": "A", "content": "safe", "frontmatter": note["frontmatter"]}])
    conflict = build_human_ui_bridge_preview(project="fixture", obsidian_import=[{"entity_id": "a", "title": "A", "content": "tampered", "frontmatter": note["frontmatter"]}])
    rejected = build_human_ui_bridge_preview(project="fixture", obsidian_import=[{"entity_id": "a", "content": "unmarked", "frontmatter": {}}])
    assert accepted["obsidian_import"]["accepted"]
    assert conflict["obsidian_import"]["conflicts"]
    assert rejected["obsidian_import"]["rejected"]
