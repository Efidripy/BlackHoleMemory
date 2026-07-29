from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-live-retrieval-quality.ps1"


def test_live_retrieval_quality_contract_is_read_only_and_bounded():
    text = SCRIPT.read_text(encoding="utf-8")

    for marker in (
        "context/compile",
        "retrieval/explain",
        "empty_contour_rate",
        "mutation = $false",
        "sqlite-authoritative",
        "projection-only",
        "health/slo",
        "profile = \"low-context\"",
        "minimum_expected_included",
    ):
        assert marker in text


def test_live_retrieval_quality_does_not_mutate_runtime_or_vector_state():
    text = SCRIPT.read_text(encoding="utf-8").lower()
    for forbidden in ("set_payload", "upsert", "delete_collection", "stop-process", "run-bhm-projection-worker"):
        assert forbidden not in text
