from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "apply-bhm-qdrant-user-scope-backfill.py"


def test_native_acceptance_tool_is_backup_first_and_explicit():
    text = SCRIPT.read_text(encoding="utf-8")
    for marker in (
        'choices=("plan", "apply", "rollback")',
        "--confirm",
        "--backup-dir",
        "_write_backup",
        "payload_sha256",
        "set_payload",
        "overwrite_payload",
        "requires_confirm",
        "mutation",
    ):
        assert marker in text
