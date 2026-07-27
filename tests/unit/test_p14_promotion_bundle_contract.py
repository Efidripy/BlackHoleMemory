from __future__ import annotations

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_promotion_bundle_is_local_and_fail_closed():
    script = (REPO_ROOT / "scripts" / "prepare-bhm-promotion-bundle.ps1").read_text(encoding="utf-8")
    verifier = (REPO_ROOT / "scripts" / "verify-bhm-promotion-bundle.ps1").read_text(encoding="utf-8")
    for marker in (
        "prepared-not-published",
        "verify-release-build.py",
        "verify-release-trust.py",
        "Assert-OutputSafe",
        "external_actions_performed = $false",
        "sidecar_match",
        "promotion-manifest.json",
    ):
        assert marker in script
    assert "git tag" not in script.lower()
    assert "git push" not in script.lower()
    assert "mutation = $false" in verifier
    assert "promotion state is not prepared-not-published" in verifier
    assert "verify-release-trust.py" in verifier


def test_release_notes_keep_external_publication_operator_only():
    notes = (REPO_ROOT / "docs" / "releases" / "bhm-v1.7.1-release-notes.md").read_text(encoding="utf-8")
    assert "prepared-not-published" in notes
    assert re.search(r"(?im)^sha256:\s*[0-9a-f]{64}\s*$", notes)
    assert "Tag, push и внешняя публикация не выполнялись" in notes
