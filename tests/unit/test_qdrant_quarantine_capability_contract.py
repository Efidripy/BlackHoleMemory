from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "bhm_quarantine_projection_orphans.py"


def test_qdrant_quarantine_apply_and_restore_require_admin_capability():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "configured_admin_capability" in text
    assert "destructive Qdrant quarantine/restore requires BHM_ADMIN_CAPABILITY" in text
    assert "if (args.apply or args.restore_manifest)" in text
    assert "adminCapabilityConfigured" in text

