from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("validate_ci_receipt_binding", ROOT / "scripts" / "validate-ci-receipt-binding.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


SHA = "0123456789abcdef0123456789abcdef01234567"
TREE = "fedcba9876543210fedcba9876543210fedcba98"


def _receipt(**overrides):
    values = {
        "expected_sha": SHA,
        "observed_sha": SHA,
        "tree_sha": TREE,
        "dirty": False,
        "tool_versions": {"python": "Python 3.12", "ruff": "ruff 0.0", "uv": "uv 0.0"},
    }
    values.update(overrides)
    return MODULE.build_receipt(**values)


def test_receipt_binds_revision_tree_toolchain_and_no_write_execution():
    receipt = _receipt()

    assert receipt["schema_version"] == "bhm.ci.receipt-binding.v1"
    assert receipt["binding"] == {
        "expected_sha": SHA,
        "observed_sha": SHA,
        "sha_match": True,
        "tree_sha": TREE,
        "worktree_clean": True,
    }
    assert MODULE.validate_receipt(receipt) == []
    assert receipt["execution"] == {
        "writes_sqlite_state": False,
        "writes_qdrant": False,
        "writes_source": False,
        "network_used": False,
    }


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"observed_sha": "f" * 40}, "expected and observed Git SHA differ"),
        ({"dirty": True}, "worktree is not clean"),
    ],
)
def test_receipt_fails_closed_on_revision_or_dirty_tree(overrides, expected):
    assert expected in MODULE.validate_receipt(_receipt(**overrides))


def test_receipt_rejects_short_sha():
    with pytest.raises(MODULE.ReceiptBindingError, match="full 40-character"):
        _receipt(expected_sha="abc")


def test_receipt_rejects_missing_tool_version():
    receipt = _receipt(tool_versions={"python": "unavailable", "ruff": "ruff 0.0", "uv": "uv 0.0"})

    assert "tool_versions.python is unavailable" in MODULE.validate_receipt(receipt)
