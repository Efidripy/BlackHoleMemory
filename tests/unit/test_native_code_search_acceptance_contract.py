from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-native-code-search-acceptance.py"


def test_native_code_search_acceptance_is_explicit_and_read_only() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for marker in (
        '"operation": "code_search"',
        '"search_mode": mode',
        '"project": project',
        '"root": normalized_root',
        '"semantic_fusion": False',
        '"writes_qdrant": False',
        '"index_refresh": False',
        '"restart": False',
        "explicit_operator_flag_and_fresh_snapshot_required",
    ):
        assert marker in text


def test_native_code_search_acceptance_covers_all_four_modes() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for mode in ("text", "path", "symbol", "metadata"):
        assert f'("{mode}",' in text


def test_native_code_search_acceptance_uses_registry_http_timeout() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "BHM_INTERNAL_HTTP_TIMEOUT_SECONDS" in text
    assert "timeout=20.0" not in text
