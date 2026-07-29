from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-native-retrieval-acceptance.py"


def test_native_acceptance_contract_is_read_only_and_bounded():
    text = SCRIPT.read_text(encoding="utf-8")
    for marker in (
        "native-mem0-qdrant",
        "user_id",
        '"data"',
        "mutation",
        "memory.search",
        "compatibility fallback is not called",
    ):
        assert marker in text


def test_native_acceptance_has_no_mutating_qdrant_calls():
    text = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("set_payload", "overwrite_payload", "upsert", "delete"):
        assert forbidden not in text
