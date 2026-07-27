from __future__ import annotations

from datetime import datetime, timezone

import pytest

from blackholememory.documentation_factory import DocumentationFactoryError
from blackholememory.documentation_factory import build_documentation_factory_preview
from blackholememory.documentation_factory import verify_documentation_factory_digest


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _documents() -> list[dict]:
    return [
        {"path": "README.md", "content": "# Project\n\nSee [missing](references/missing.md).\n"},
        {"path": "references/architecture/0121-demo.md", "content": "# Architecture\n\n## Status\nAccepted\n\n## Decision\nUse bounded patches.\n"},
        {"path": "references/operations/demo.md", "content": "# Operations\n\n## Evidence\npassed\n"},
    ]


def test_preview_builds_findings_patches_gates_and_digest():
    preview = build_documentation_factory_preview(_documents(), project="demo", now=NOW)

    assert preview["schema_version"] == "bhm.llm.documentation-factory.v1"
    assert preview["findings"]
    assert preview["patches"]
    assert preview["gates"]["link_gate"] is False
    assert preview["gates"]["patch_review_required"] is True
    assert preview["execution"]["documents_written"] is False
    assert verify_documentation_factory_digest(preview) is True


def test_vision_requires_confirmed_capability_and_never_starts_ocr():
    disabled = build_documentation_factory_preview(
        _documents(),
        vision_assets=[{"path": "screens/home.png"}],
        vision_confirmed=False,
        now=NOW,
    )
    enabled = build_documentation_factory_preview(
        _documents(),
        vision_assets=[{"path": "screens/home.png"}],
        vision_confirmed=True,
        now=NOW,
    )

    assert disabled["vision"]["status"] == "disabled_unconfirmed_capability"
    assert enabled["vision"]["status"] == "confirmed_preview"
    assert enabled["vision"]["critiques"][0]["ocr_performed"] is False
    assert enabled["execution"]["vision_started"] is False


def test_required_sections_and_localization_are_explicit_findings():
    preview = build_documentation_factory_preview(
        [{"path": "README.md", "content": "# Project\n"}],
        locale="ru-RU",
        now=NOW,
    )

    codes = {item["code"] for item in preview["findings"]}
    assert "missing_section" in codes
    assert preview["localization"]


def test_feature_flags_disable_patches_and_unknown_flags_fail_closed():
    flags = {name: False for name in ("readme", "adr", "changelog", "release", "runbook", "migration", "localization", "vision")}
    preview = build_documentation_factory_preview(_documents(), feature_flags=flags, now=NOW)

    assert preview["patches"] == []
    assert preview["localization"] == []
    assert preview["vision"]["status"] == "disabled_by_feature_flag"
    with pytest.raises(DocumentationFactoryError):
        build_documentation_factory_preview([], feature_flags={"unknown": True})


def test_clean_document_set_has_green_link_and_section_gates():
    preview = build_documentation_factory_preview(
        [
            {"path": "README.md", "content": "# Status\nP17.16\n\n# Architecture\nSee [reference](references/architecture/demo.md).\n"},
            {"path": "references/architecture/demo.md", "content": "# Status\nAccepted\n# Decision\nBounded.\n# Rollback\nRemove.\n"},
        ],
        now=NOW,
    )

    assert preview["gates"]["link_gate"] is True
    assert preview["gates"]["section_gate"] is True


def test_bounds_fail_closed():
    with pytest.raises(DocumentationFactoryError):
        build_documentation_factory_preview(_documents(), max_patches=0)
    with pytest.raises(DocumentationFactoryError):
        build_documentation_factory_preview(_documents() * 22)
